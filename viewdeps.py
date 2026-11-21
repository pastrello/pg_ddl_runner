"""Conservative catalog-assisted recreation of ordinary views (PostgreSQL 13+)."""
import datetime
import json
import os
from pathlib import Path
import tempfile
import logging

LOG = logging.getLogger('pgddl')

class DependencyError(Exception):
    pass


def topo(nodes, edges):
    """Parents before dependent children, no recursion/path explosion."""
    pending = {n: set() for n in nodes}
    for parent, child in edges:
        if parent != child and parent in pending and child in pending:
            pending[child].add(parent)
    result = []
    while pending:
        ready = sorted(n for n, parents in pending.items() if not parents)
        if not ready:
            raise DependencyError('Ciclo entre views: reconstrução automática recusada.')
        result.extend(ready)
        for n in ready: del pending[n]
        for parents in pending.values(): parents.difference_update(ready)
    return result


def discover(conn, oid, col, only=False):
    rows = conn.execute('''WITH RECURSIVE base(oid) AS (
        SELECT %s::oid UNION SELECT i.inhrelid FROM pg_catalog.pg_inherits i JOIN base b ON b.oid=i.inhparent WHERE NOT %s::boolean
      ), edges AS (
        SELECT DISTINCT d.refobjid AS parent,r.ev_class AS child,d.refobjsubid AS subid
        FROM pg_catalog.pg_depend d JOIN pg_catalog.pg_rewrite r ON r.oid=d.objid
        JOIN pg_catalog.pg_class c ON c.oid=r.ev_class
        WHERE d.classid='pg_catalog.pg_rewrite'::regclass AND d.refclassid='pg_catalog.pg_class'::regclass
          AND r.rulename='_RETURN' AND c.relkind IN ('v','m') AND d.refobjid<>r.ev_class
      ), seed AS (
        SELECT DISTINCT e.child FROM edges e JOIN base b ON b.oid=e.parent
        JOIN pg_catalog.pg_attribute a ON a.attrelid=b.oid AND a.attnum=e.subid
        WHERE a.attname=%s AND NOT a.attisdropped
      ), nodes(oid) AS (
        SELECT child FROM seed UNION SELECT e.child FROM edges e JOIN nodes n ON n.oid=e.parent
      ) SELECT DISTINCT c.oid,ns.nspname,c.relname,c.relkind,pg_catalog.pg_get_userbyid(c.relowner),
        c.relispopulated, EXISTS(SELECT 1 FROM seed s WHERE s.child=c.oid), e.parent
      FROM nodes n JOIN pg_catalog.pg_class c ON c.oid=n.oid
      JOIN pg_catalog.pg_namespace ns ON ns.oid=c.relnamespace
      LEFT JOIN edges e ON e.child=c.oid AND e.parent IN (SELECT oid FROM nodes)
      ORDER BY c.oid,e.parent''',(oid,only,col)).fetchall()
    nodes, edges = {}, set()
    for oid, ns, name, kind, owner, populated, direct, parent in rows:
        nodes[oid] = dict(oid=oid,schema=ns,name=name,kind=kind,owner=owner,populated=populated,direct=direct)
        if parent is not None: edges.add((parent,oid))
    return [nodes[n] for n in topo(nodes,edges)], sorted(edges)


def report(nodes, step):
    for n in nodes:
        if n['kind']=='m':
            LOG.warning('[%04d] !!! MATERIALIZED VIEW !!! %s.%s owner=%s populada=%s | RECONSTRUÇÃO AUTOMÁTICA BLOQUEADA',step,n['schema'],n['name'],n['owner'],n['populated'])
        else:
            LOG.warning('[%04d] DEPENDÊNCIA VIEW %s.%s owner=%s (%s)',step,n['schema'],n['name'],n['owner'],'direta' if n['direct'] else 'indireta')
    if nodes: LOG.warning('[%04d] DEPENDÊNCIAS: views=%s MATERIALIZED VIEWS=%s',step,sum(n['kind']=='v' for n in nodes),sum(n['kind']=='m' for n in nodes))


def acl(conn, oid):
    return conn.execute('''SELECT 0 AS attnum, pg_catalog.pg_get_userbyid(x.grantor) AS grantor,
        CASE WHEN x.grantee=0 THEN NULL ELSE pg_catalog.pg_get_userbyid(x.grantee) END AS grantee,x.privilege_type,x.is_grantable
      FROM pg_catalog.pg_class c CROSS JOIN LATERAL pg_catalog.aclexplode(coalesce(c.relacl,pg_catalog.acldefault('r',c.relowner))) x WHERE c.oid=%s
      UNION ALL SELECT a.attnum,pg_catalog.pg_get_userbyid(x.grantor),CASE WHEN x.grantee=0 THEN NULL ELSE pg_catalog.pg_get_userbyid(x.grantee) END,x.privilege_type,x.is_grantable
      FROM pg_catalog.pg_attribute a CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) x WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped
      ORDER BY 1,2,3,4,5''',(oid,oid)).fetchall()


def snapshot(conn, node):
    oid=node['oid']
    row=conn.execute('''SELECT pg_catalog.pg_get_viewdef(c.oid,false),c.reloptions,pg_catalog.obj_description(c.oid,'pg_class'),
      pg_catalog.pg_get_userbyid(c.relowner), c.relkind,c.relispopulated,
      EXISTS(SELECT 1 FROM pg_catalog.pg_trigger WHERE tgrelid=c.oid),
      EXISTS(SELECT 1 FROM pg_catalog.pg_rewrite WHERE ev_class=c.oid AND rulename<>'_RETURN'),
      EXISTS(SELECT 1 FROM pg_catalog.pg_seclabel WHERE classoid='pg_catalog.pg_class'::regclass AND objoid=c.oid),
      EXISTS(SELECT 1 FROM pg_catalog.pg_depend WHERE classid='pg_catalog.pg_class'::regclass AND objid=c.oid AND deptype IN ('e','x')),
      EXISTS(SELECT 1 FROM pg_catalog.pg_depend d WHERE d.refclassid='pg_catalog.pg_class'::regclass AND d.refobjid=c.oid AND d.deptype IN ('a','i')
        AND NOT (d.classid='pg_catalog.pg_type'::regclass AND d.objid=c.reltype)
        AND NOT (d.classid='pg_catalog.pg_rewrite'::regclass AND EXISTS(SELECT 1 FROM pg_catalog.pg_rewrite r WHERE r.oid=d.objid AND r.ev_class=c.oid AND r.rulename='_RETURN')))
      FROM pg_catalog.pg_class c WHERE c.oid=%s''',(oid,)).fetchone()
    if not row: raise DependencyError('View desapareceu durante a análise; repita.')
    definition,options,comment,owner,kind,populated,*flags=row
    if (owner,kind)!=(node['owner'],node['kind']): raise DependencyError('View modificada durante a análise; repita.')
    cols=conn.execute('''SELECT attnum,attname,atttypid,atttypmod,attcollation,pg_catalog.format_type(atttypid,atttypmod),
      pg_catalog.col_description(attrelid,attnum),atthasdef
      FROM pg_catalog.pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped ORDER BY attnum''',(oid,)).fetchall()
    grants=acl(conn,oid)
    reasons=[label for flag,label in zip(flags,('triggers','regras adicionais','security labels','vínculo com extensão','dependência automática/interna adicional')) if flag]
    if any(c[7] for c in cols): reasons.append('default de coluna')
    if [c[0] for c in cols]!=list(range(1,len(cols)+1)): reasons.append('numeração de colunas não contínua')
    if any(g[1]!=owner for g in grants): reasons.append('GRANT com grantor diferente do owner')
    if any(g[3] not in ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN') for g in grants): reasons.append('privilégio desconhecido')
    if any(o.split('=',1)[0] not in ('security_barrier','security_invoker','check_option') or '=' not in o for o in options or []): reasons.append('opção de view não suportada')
    if kind=='m': reasons.append('MATERIALIZED VIEW: dados e índices não serão reconstruídos')
    if not definition: reasons.append('definição indisponível')
    indexes=conn.execute('SELECT pg_catalog.pg_get_indexdef(indexrelid) FROM pg_catalog.pg_index WHERE indrelid=%s ORDER BY indexrelid',(oid,)).fetchall()
    if indexes and kind=='v': reasons.append('índices inesperados')
    return dict(node,definition=definition,options=options or [],comment=comment,columns=cols,acl=grants,issues=reasons,indexes=[r[0] for r in indexes],populated=populated)


def qname(n):
    from psycopg import sql
    return sql.Identifier(n['schema'],n['name'])


def statements(conn, n):
    """Restoration SQL for supported views. No DROP in the backup file."""
    from psycopg import sql
    if n['issues']: raise DependencyError('Metadados não suportados: '+', '.join(n['issues']))
    q=qname(n); owner=sql.Identifier(n['owner'])
    cols=sql.SQL(', ').join(sql.Identifier(c[1]) for c in n['columns'])
    definition=n['definition'].rstrip().removesuffix(';')
    commands=[sql.SQL('CREATE VIEW {} ({}) AS {}').format(q,cols,sql.SQL(definition)),
              sql.SQL('ALTER VIEW {} OWNER TO {}').format(q,owner)]
    if n['options']:
        opts=sql.SQL(', ').join(sql.SQL('{} = {}').format(sql.Identifier(o.split('=',1)[0]),sql.Literal(o.split('=',1)[1])) for o in n['options'])
        commands.append(sql.SQL('ALTER VIEW {} SET ({})').format(q,opts))
    if n['comment'] is not None: commands.append(sql.SQL('COMMENT ON VIEW {} IS {}').format(q,sql.Literal(n['comment'])))
    for c in n['columns']:
        if c[6] is not None: commands.append(sql.SQL('COMMENT ON COLUMN {}.{} IS {}').format(q,sql.Identifier(c[1]),sql.Literal(c[6])))
    commands.append(sql.SQL('SET LOCAL ROLE {}').format(owner))
    # Remove any current defaults before applying the exact effective ACL.
    # Format qualified object names with %I; literal names inside DO body are quoted by SQL.Literal.
    body=sql.SQL('''DECLARE r record; rel oid; BEGIN
      SELECT c.oid INTO STRICT rel FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
       WHERE n.nspname={} AND c.relname={};
      FOR r IN SELECT DISTINCT x.grantee FROM pg_catalog.pg_class c CROSS JOIN LATERAL
       pg_catalog.aclexplode(coalesce(c.relacl,pg_catalog.acldefault('r',c.relowner))) x WHERE c.oid=rel
      LOOP EXECUTE pg_catalog.format('REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM %s RESTRICT',{}, {},
       CASE WHEN r.grantee=0 THEN 'PUBLIC' ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(r.grantee)) END); END LOOP;
      END;''').format(sql.Literal(n['schema']),sql.Literal(n['name']),sql.Literal(n['schema']),sql.Literal(n['name'])).as_string(conn)
    commands.append(sql.SQL('DO {}').format(sql.Literal(body)))
    colnames={c[0]:c[1] for c in n['columns']}
    for attnum,grantor,grantee,priv,grantable in n['acl']:
        # Snapshot has already allowlisted privileges and grantors.
        p=sql.SQL(priv) if not attnum else sql.SQL('{} ({})').format(sql.SQL(priv),sql.Identifier(colnames[attnum]))
        commands.append(sql.SQL('GRANT {} ON TABLE {} TO {}{}').format(p,q,sql.Identifier(grantee) if grantee is not None else sql.SQL('PUBLIC'),sql.SQL(' WITH GRANT OPTION' if grantable else '')))
    commands.append(sql.SQL('RESET ROLE'))
    return commands


def write_backup(conn, nodes, edges, args, step, manifest, reason=None):
    root=Path(args.backup_dir or (Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'pgddl'/'views'))
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    stamp=datetime.datetime.now().astimezone()
    directory=Path(tempfile.mkdtemp(prefix=stamp.strftime('%d-%m-%Y_%H:%M')+f'_step-{step.number:04d}_',dir=root))
    def save(name,content):
        with os.fdopen(os.open(directory/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w',encoding='utf-8') as f:
            f.write(content); f.flush(); os.fsync(f.fileno())
    info=dict(timestamp=stamp.isoformat(),database=args.dbname,schema=args.schema,manifest_sha256=manifest.digest,step=step.number,ddl=step.sql,reason=reason,edges=edges,views=nodes)
    save('views.json',json.dumps(info,ensure_ascii=False,indent=2)+'\n')
    if reason or any(n['issues'] for n in nodes):
        save('LEIA-ME.txt','EXPORTAÇÃO DE DIAGNÓSTICO. Não é backup completo nem script de restauração. Materialized views: dados não exportados. Use pg_dump e resolução manual.\n')
    else:
        script=['-- Restauração apenas de views ausentes, na ordem de dependência. Revisar antes de executar.','BEGIN;','SET LOCAL search_path TO pg_catalog;','SET LOCAL standard_conforming_strings = on;']
        for n in nodes: script.extend(c.as_string(conn)+';' for c in statements(conn,n))
        script.append('COMMIT;')
        save('restore-views.sql','\n'.join(script)+'\n')
    # Persist directory entries before any DROP.
    for p in (directory,root):
        fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    LOG.info('BACKUP DE VIEWS: %s',directory)
    return str(directory)


def interface_ok(old,new):
    # Existing names, types and collation, except a widening of built-in varchar.
    if len(old)!=len(new): return False
    for a,b in zip(old,new):
        if a[:3]!=b[:3] or a[4]!=b[4] or a[6:]!=b[6:]: return False
        if a[3]!=b[3] and not (a[2]==1043 and a[3]>=4 and (b[3]>=a[3] or b[3]==-1)): return False
    return True


def process(conn, nodes, edges, args, step, manifest, rebuild=False):
    """Called inside caller's transaction; checkpoint remains in that transaction."""
    from psycopg import sql
    if not nodes: return None
    report(nodes,step.number)
    before_path=conn.execute("SELECT current_setting('search_path')").fetchone()[0]
    conn.execute('SET LOCAL search_path TO pg_catalog')
    reason=None
    if any(n['kind']=='m' for n in nodes): reason='MATERIALIZED VIEW presente; resolução manual obrigatória.'
    if rebuild and not reason:
        role_ok=conn.execute('SELECT session_user=current_user AND r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname=current_user').fetchone()[0]
        if not role_ok: reason='Reconstrução automática exige superusuário sem SET ROLE prévio.'
        elif args._hierarchy: reason='Tabela com herança/partições: reconstrução automática desabilitada.'
    if rebuild and not reason:
        if args.lock_timeout <= 0:
            reason='Reconstrução exige --lock-timeout positivo.'
        else:
            # LOCK TABLE on views recursively locks underlying relations and checks
            # their privileges as the view owner. Do not use it here (0.4.1).
            # The caller holds the base table prelock; DROP VIEW takes its own locks.
            LOG.info('Pré-lock somente na tabela alvo; DROP VIEW com lock_timeout=%ss. Não execute DDL concorrente nas views.',args.lock_timeout)
            check_nodes, check_edges=args._rediscover()
            if check_nodes!=nodes or check_edges!=edges: raise DependencyError('Grafo de views mudou; nenhuma alteração aplicada.')
    captured=[snapshot(conn,n) for n in nodes]
    for n in captured:
        if n['kind']=='m': LOG.warning('!!! MATERIALIZED VIEW !!! %s.%s: índices=%d; conteúdo não exportado.',n['schema'],n['name'],len(n['indexes']))
        if n['issues']: LOG.warning('VIEW %s.%s: %s',n['schema'],n['name'],', '.join(n['issues']))
    path=write_backup(conn,captured,edges,args,step,manifest,reason)
    if not rebuild:
        conn.execute("SELECT set_config('search_path',%s,true)",(before_path,))
        return path
    if reason or any(n['issues'] for n in captured): raise DependencyError(f'Reconstrução recusada: {reason or "metadados não suportados"} | backup={path}')
    # Recheck after local I/O. This detects visible changes, not a full guard
    # against concurrent external DDL: a maintenance window is required.
    check_nodes, check_edges=args._rediscover()
    if check_nodes!=nodes or check_edges!=edges: raise DependencyError(f'Grafo de views mudou após backup={path}; nenhuma alteração aplicada.')
    if [snapshot(conn,n) for n in nodes]!=captured:
        raise DependencyError(f'Metadados mudaram após backup={path}; nenhuma alteração aplicada.')
    # Only this explicit policy can drop; RESTRICT never removes normal external dependencies.
    for n in reversed(captured):
        LOG.info('VIEW BEGIN DROP VIEW RESTRICT %s.%s',n['schema'],n['name'])
        conn.execute(sql.SQL('DROP VIEW {} RESTRICT').format(qname(n)))
        LOG.info('VIEW OK DROP VIEW RESTRICT %s.%s',n['schema'],n['name'])
    conn.execute("SELECT set_config('search_path',%s,true)",(before_path,))
    LOG.info('VIEW BEGIN DDL ORIGINAL passo=%04d',step.number)
    conn.execute(step.sql,prepare=True)
    LOG.info('VIEW OK DDL ORIGINAL passo=%04d',step.number)
    conn.execute('SET LOCAL search_path TO pg_catalog')
    for n in captured:
        for command in statements(conn,n):
            # Log the operation label, not the full view definition or comments.
            words=command.as_string(conn).split()
            label=' '.join(words[:2]) if words[0] not in ('DO','GRANT') else words[0]
            LOG.info('VIEW BEGIN %s %s.%s',label,n['schema'],n['name'])
            conn.execute(command)
            LOG.info('VIEW OK %s %s.%s',label,n['schema'],n['name'])
        LOG.info('VIEW BEGIN VALIDATE %s.%s',n['schema'],n['name'])
        oid=conn.execute('SELECT c.oid FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace ns ON ns.oid=c.relnamespace WHERE ns.nspname=%s AND c.relname=%s',(n['schema'],n['name'])).fetchone()[0]
        new=snapshot(conn,dict(n,oid=oid))
        if (new['issues'] or not interface_ok(n['columns'],new['columns']) or n['acl']!=new['acl'] or sorted(n['options'])!=sorted(new['options']) or n['comment']!=new['comment'] or n['definition']!=new['definition']):
            raise DependencyError(f'Validação de view divergente: {n["schema"]}.{n["name"]}; rollback obrigatório.')
        for a,b in zip(n['columns'],new['columns']):
            if a[5]!=b[5]: LOG.warning('INTERFACE VIEW %s.%s.%s: %s -> %s',n['schema'],n['name'],a[1],a[5],b[5])
        LOG.info('VIEW RECRIADA E VALIDADA %s.%s',n['schema'],n['name'])
    conn.execute("SELECT set_config('search_path',%s,true)",(before_path,))
    return path
