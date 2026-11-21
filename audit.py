"""Read-only audit helpers; no database schema changes."""
import collections
import datetime
import html
import os
from pathlib import Path
import tempfile
import platform_paths

HISTORY = '_pgddl_runner'

def collect(conn, key, m, n):
    applied = []
    errors = []
    migrations = []
    if n is not None:
        for row in conn.execute(f'''SELECT step,mode,elapsed_ms,detail,applied_at FROM {HISTORY}.applied
            WHERE target_schema=%s AND migration_id=%s ORDER BY step''', key).fetchall():
            step,mode,ms,detail,stamp = row
            s=m.steps[step-1]
            applied.append(dict(step=step,line=s.line,kind=s.kind,sql=s.sql,mode=mode,ms=ms,detail=detail,stamp=str(stamp)))
        for step,state,msg,stamp in conn.execute(f'''SELECT step,sqlstate,message,occurred_at FROM {HISTORY}.errors
            WHERE target_schema=%s AND migration_id=%s ORDER BY occurred_at DESC,step''',key).fetchall():
            errors.append(dict(step=step,state=state,message=msg,stamp=str(stamp)))
    if conn.execute('SELECT to_regclass(%s)',(f'{HISTORY}.migrations',)).fetchone()[0]:
        migrations=conn.execute(f'''SELECT m.migration_id,m.total,count(a.step),m.created_at,m.file_sha256,
          count(a.step) FILTER (WHERE a.mode='acknowledged') FROM {HISTORY}.migrations m
          LEFT JOIN {HISTORY}.applied a USING(target_schema,migration_id)
          WHERE m.target_schema=%s GROUP BY m.migration_id,m.total,m.created_at,m.file_sha256 ORDER BY m.created_at DESC''',(key[0],)).fetchall()
    return dict(applied=applied,errors=errors,migrations=migrations,registered=n is not None,total=len(m.steps))

def summarize(data):
    a=data['applied']; e=data['errors']; modes={x['step']:x['mode'] for x in a}
    for error in e:
        mode=modes.get(error['step'])
        error['status']={'executed':'RESOLVIDA','acknowledged':'RECONHECIDA MANUALMENTE'}.get(mode,'PENDENTE')
    counts=collections.Counter(modes.values())
    ec=collections.Counter(x['status'] for x in e)
    kinds={}
    for x in a:
        if x['mode']!='executed': continue
        k=kinds.setdefault(x['kind'],{'count':0,'ms':0}); k['count']+=1; k['ms']+=x['ms']
    pending=data['total']-len(a)
    status=('NÃO REGISTRADA' if not data['registered'] else 'PENDENTE' if pending else 'CONCLUÍDA COM RECONHECIMENTOS MANUAIS' if counts['acknowledged'] else 'CONCLUÍDA COM SUCESSO')
    return dict(status=status,executed=counts['executed'],acknowledged=counts['acknowledged'],pending=pending,
        unresolved=ec['PENDENTE'],resolved=ec['RESOLVIDA'],manual=ec['RECONHECIDA MANUALMENTE'],
        recovered=len({x['step'] for x in e if x['status']=='RESOLVIDA'}),kinds=kinds,
        elapsed_ms=sum(k['ms'] for k in kinds.values()),
        retries=sum(max(0,x['detail'].get('lock_attempts',1)-1) for x in a if x['mode']=='executed'),
        views=sum(x['detail'].get('rebuilt_views',0) for x in a if x['mode']=='executed'))

def status_log(log,data,summary,m,history=False):
    s=summary; log.info('STATUS: %s',s['status'])
    n=len(data['applied']); log.info('Confirmados: %d/%d (%.1f%%)',n,data['total'],100*n/data['total'])
    log.info('Executados: %d | Reconhecidos (ack): %d | Pendentes: %d',s['executed'],s['acknowledged'],s['pending'])
    log.info('Falhas pendentes: %d | Falhas resolvidas: %d | Falhas reconhecidas manualmente: %d | Comandos recuperados: %d',s['unresolved'],s['resolved'],s['manual'],s['recovered'])
    if n<data['total']:
        step=m.steps[n]; log.info('Próximo: [%d] linha %d %s',step.number,step.line,' '.join(step.sql.split())[:180])
    rows=data['errors'] if history else [e for e in data['errors'] if e['status']=='PENDENTE'][:5]
    for e in rows:
        fn=log.warning if e['status']=='PENDENTE' else log.info
        fn('%s [%d] %s %s %s',e['status'],e['step'],e['state'] or '-',e['stamp'],e['message'])
    if not history and (s['resolved'] or s['manual']): log.info('Ocorrências anteriores preservadas; use --history para consultar.')
    if not history and s['unresolved']>5: log.info('Exibindo 5 falhas pendentes mais recentes; use --history.')

def configure(conn, args, log):
    for name,val in [('max_parallel_maintenance_workers',args.parallel_maintenance_workers),('maintenance_work_mem',args.maintenance_work_mem)]:
        if val is not None:
            conn.execute('SELECT set_config(%s,%s,false)',(name,str(val)))
    settings=dict(conn.execute('''SELECT name,setting || COALESCE(unit,'') FROM pg_settings WHERE name IN
        ('max_worker_processes','max_parallel_workers','max_parallel_maintenance_workers','maintenance_work_mem')''').fetchall())
    log.info('Manutenção (sessão): %s | ordem sequencial preservada','; '.join(f'{k}={v}' for k,v in sorted(settings.items())))
    return settings

def validate(conn,m,applied,schema,target,canonical_name,column_request,index_request):
    """Checks final *confirmed* expectations, not full SQL equivalence."""
    expectations={}
    for a in applied:
        s=m.steps[a['step']-1]; t=target(s); rel=canonical_name(t.name,schema)
        if s.kind.startswith('ALTER TABLE'):
            col=column_request(s)
            k=('column',rel,col[0]) if col else ('unsupported',s.number)
        elif s.kind in ('CREATE INDEX','CREATE UNIQUE INDEX'):
            idx=index_request(s)
            k=('index',rel[0],idx[0]) if idx else ('unsupported',s.number)
        else: k=('relation',rel,s.kind.startswith('ALTER SEQUENCE') or s.kind=='CREATE SEQUENCE')
        expectations[k]=(s,rel)
    result=[]
    cache={}
    for k,(s,rel) in expectations.items():
        def add(status,message): result.append(dict(step=s.number,status=status,object='.'.join(rel),message=message))
        if k[0]=='unsupported': add('NÃO VERIFICADO','Expressão ou definição não suportada pelo verificador.'); continue
        if rel not in cache:
            cache[rel]=conn.execute('''SELECT c.oid,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relname=%s''',rel).fetchone()
        info=cache[rel]
        expected_kinds=('S',) if k[0]=='relation' and k[2] else ('r','p')
        if not info or info[1] not in expected_kinds:
            add('DIVERGÊNCIA','Objeto ausente ou tipo de objeto diferente.'); continue
        oid=info[0]
        if k[0]=='relation':
            add('PARCIAL','Existência e tipo de objeto confirmados; não verifica conteúdo, definição completa ou valor da sequence.'); continue
        if k[0]=='column':
            name,expected=column_request(s)
            actual=conn.execute('''SELECT format_type(atttypid,atttypmod) FROM pg_attribute
                WHERE attrelid=%s AND attname=%s AND attnum>0 AND NOT attisdropped''',(oid,name)).fetchone()
            if not actual: add('DIVERGÊNCIA',f'Coluna {name} ausente.')
            elif expected is None: add('PARCIAL',f'Coluna {name} existe; tipo solicitado não interpretado.')
            elif actual[0]!=expected: add('DIVERGÊNCIA',f'{name}: esperado {expected}; atual {actual[0]}.')
            else: add('OK',f'{name}: existência e tipo {expected} confirmados (não verifica defaults/constraints).')
        else:
            name,cols,unique=index_request(s)
            row=conn.execute('''SELECT i.indisunique,i.indisvalid AND i.indisready AND i.indislive, am.amname,
                i.indpred IS NULL AND i.indexprs IS NULL AND i.indnatts=i.indnkeyatts
                AND NOT EXISTS (SELECT 1 FROM unnest(i.indoption::smallint[]) AS o(opt) WHERE o.opt<>0),
                ARRAY(SELECT a.attname FROM generate_series(0,i.indnkeyatts::integer-1) g(k)
                    LEFT JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[g.k] ORDER BY g.k)
                FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_am am ON am.oid=c.relam
                JOIN pg_namespace ns ON ns.oid=c.relnamespace WHERE i.indrelid=%s AND ns.nspname=%s AND c.relname=%s''',(oid,rel[0],name)).fetchone()
            if not row or tuple(row[:4])!=(unique,True,'btree',True) or tuple(row[4])!=cols:
                add('DIVERGÊNCIA',f'Índice {name}: ausente, inválido ou chaves/propriedades básicas divergentes.')
            else: add('OK',f'Índice {name}: chaves, unicidade, método e validade conferidos; não verifica opclasses/collations.')
    return result

def render(data,s,identity,validation=None):
    e=lambda v: html.escape(str(v if v is not None else '—'))
    def table(headers,rows):
        return '<table><thead><tr>'+''.join('<th>'+e(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table>'
    sections=[f'<h1>PG DDL Runner 0.5.1 — relatório</h1><h2>{e(s["status"])}</h2>',table(('Identificação','Valor'),identity.items()),
      table(('Indicador','Valor'),[(label,s[k]) for label,k in [('Executados','executed'),('Reconhecidos manualmente','acknowledged'),('Pendentes','pending'),('Falhas pendentes','unresolved'),('Falhas resolvidas','resolved'),('Falhas reconhecidas manualmente','manual'),('Passos recuperados','recovered'),('Tempo acumulado (ms)','elapsed_ms'),('Retries confirmados','retries'),('Reconstruções de views (contagem)','views')]]),
      '<p>Tempos registrados por comando incluem espera/retry e excluem o COMMIT final, pausas e tentativas definitivamente malsucedidas. Não representam duração total de parede. Retries contabilizados somente em comandos confirmados; bloqueadores e amostras históricas estão nos logs locais, não no catálogo.</p>',
      '<h2>Tempo por família</h2>',table(('Tipo','Executados','Tempo (s)'),[(k,v['count'],f"{v['ms']/1000:.3f}") for k,v in sorted(s['kinds'].items(),key=lambda x:-x[1]['ms'])]),
      '<h2>30 comandos mais demorados</h2>',table(('Passo','Linha','Tipo','Tempo (s)','SQL'),[(a['step'],a['line'],a['kind'],f"{a['ms']/1000:.3f}",a['sql']) for a in sorted((a for a in data['applied'] if a['mode']=='executed'),key=lambda x:-x['ms'])[:30]]),
      '<h2>Configurações de manutenção registradas</h2>',table(('Configurações da sessão','Passos confirmados'),collections.Counter('; '.join(f'{k}={v}' for k,v in sorted(a['detail'].get('maintenance_settings',{}).items())) or 'Não registrado nas versões anteriores' for a in data['applied'] if a['mode']=='executed').items()),
      '<h2>Reconstruções de views confirmadas</h2>',table(('Passo','Quantidade','Backup','Nomes (se registrados)'),[(a['step'],a['detail']['rebuilt_views'],a['detail'].get('view_backup'),', '.join(a['detail'].get('view_names',[])) or 'Consulte JSON do backup original') for a in data['applied'] if a['detail'].get('rebuilt_views')]),
      '<h2>Ocorrências (histórico completo)</h2>',table(('Passo','Estado','SQLSTATE','Data','Mensagem'),[(x['step'],x['status'],x['state'],x['stamp'],x['message']) for x in data['errors']]),
      '<h2>Reconhecimentos manuais</h2>',table(('Passo','Motivo','Data'),[(a['step'],a['detail'].get('reason'),a['stamp']) for a in data['applied'] if a['mode']=='acknowledged']),
      '<h2>Retries confirmados</h2>',table(('Passo','Tentativas','SQL'),[(a['step'],a['detail']['lock_attempts'],a['sql']) for a in data['applied'] if a['detail'].get('lock_attempts',1)>1]),
      '<h2>Histórico de migrações do schema</h2>',table(('Migração','Total','Confirmados','Data inicial','SHA-256','Ack'),data['migrations'])]
    if validation is not None:
        sections+=['<h2>Validação estrutural limitada</h2><p>Somente expectativas dos passos confirmados; última alteração conhecida por coluna. Não comprova equivalência integral, dados, permissões ou funcionamento do ERP.</p>',table(('Estado','Quantidade'),collections.Counter(v['status'] for v in validation).items()),table(('Passo','Objeto','Estado','Detalhe'),[(v['step'],v['object'],v['status'],v['message']) for v in validation])]
    sections+=['<h2>Todos os comandos confirmados</h2>',table(('Passo','Linha','Modo','Tempo (ms)','Data','SQL'),[(a['step'],a['line'],a['mode'],a['ms'],a['stamp'],a['sql']) for a in data['applied']])]
    return '<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>PG DDL Runner — auditoria</title><style>body{font:15px system-ui,sans-serif;color:#172334;background:#f3f6fa;max-width:1400px;margin:30px auto;padding:0 18px}h1,h2{color:#17456a}table{border-collapse:collapse;width:100%;background:white;margin:12px 0 26px;table-layout:auto}td,th{border:1px solid #d6e0e9;padding:8px;text-align:left;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}th{background:#e1eaf2}tr:nth-child(even){background:#f6f9fb}p{max-width:100ch}</style><body>'+''.join(sections)+'</body></html>'

def write_report(content, directory=None):
    root=Path(directory or (platform_paths.state_root()/'reports'))
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    stamp=platform_paths.timestamp_part()
    fd,name=tempfile.mkstemp(prefix=stamp+'_',suffix='_pgddl.html',dir=root)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f: f.write(content); f.flush(); os.fsync(f.fileno())
    except BaseException:
        Path(name).unlink(missing_ok=True); raise
    return name
