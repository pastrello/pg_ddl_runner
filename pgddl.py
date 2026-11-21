#!/usr/bin/env python3
"""PG DDL Runner 0.5.1 — executor sequencial de DDL PostgreSQL."""
import argparse
import collections
import dataclasses
import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import threading
import viewdeps
import audit
import platform_paths

VERSION = '0.5.1'
HISTORY = '_pgddl_runner'
LOCK_KEY = 726340125503884297  # global por banco, entre instâncias deste runner
LOG = logging.getLogger('pgddl')

class RunnerError(Exception):
    pass

@dataclasses.dataclass(frozen=True)
class Step:
    number: int
    line: int
    sql: str
    kind: str
    digest: str

@dataclasses.dataclass(frozen=True)
class Manifest:
    path: Path
    encoding: str
    digest: str
    steps: tuple

# Escopo propositalmente conservador: arquivo do fornecedor, não SQL arbitrário.
RULES = [
    ('ALTER TABLE ADD COLUMN', r'ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?[\w.\"]+\s+ADD\s+(?:COLUMN\s+)?'),
    ('ALTER TABLE ALTER COLUMN TYPE', r'ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?[\w.\"]+\s+ALTER\s+(?:COLUMN\s+)?[\w\"]+\s+(?:SET\s+DATA\s+)?TYPE\s+'),
    ('CREATE UNIQUE INDEX', r'CREATE\s+UNIQUE\s+INDEX\s+'),
    ('CREATE INDEX', r'CREATE\s+INDEX\s+'),
    ('CREATE TABLE', r'CREATE\s+TABLE\s+'),
    ('CREATE SEQUENCE', r'CREATE\s+SEQUENCE\s+'),
    ('ALTER SEQUENCE RESTART', r'ALTER\s+SEQUENCE\s+(?:IF\s+EXISTS\s+)?[\w.\"]+\s+RESTART(?:\s+WITH)?\s+[+-]?\d+\s*$'),
]


def split_sql(source):
    """Lexer limitado a SQL padrão, com comentários aninhados e aspas duplicadas.
    Dollar quoting e E/U& literals são rejeitados, não divididos incorretamente.
    Mantém linhas e preserva texto executável (sem comentários).
    """
    i, line, start, depth = 0, 1, None, 0
    buf = []
    quote = None
    while i < len(source):
        c = source[i]
        two = source[i:i+2]
        if depth:
            if two == '/*': depth += 1; i += 2; continue
            if two == '*/': depth -= 1; i += 2; continue
            if c == '\n': line += 1; buf.append('\n')
            i += 1; continue
        if quote:
            buf.append(c)
            if c == '\n': line += 1
            if c == quote:
                if i+1 < len(source) and source[i+1] == quote:
                    buf.append(c); i += 2; continue
                quote = None
            i += 1; continue
        if two == '--':
            end = source.find('\n', i)
            buf.append(' ')
            i = len(source) if end == -1 else end
            continue
        if two == '/*': depth = 1; buf.append(' '); i += 2; continue
        if c == '\x00' or c == '\\' or c == '$':
            raise RunnerError(f'Linha {line}: NUL, barra invertida, dollar quoting ou metacomando não suportado.')
        if start is None and not c.isspace() and c != ';': start = line
        if c in "\"'":
            if c == "'" and re.search(r'(?i)(?:\bE|\bU&)\Z', ''.join(buf)):
                raise RunnerError(f'Linha {line}: literal E/U& não suportado.')
            quote = c; buf.append(c)
        elif c == ';':
            sql = ''.join(buf).strip()
            if sql: yield start, sql
            buf = []; start = None
        else:
            buf.append(c)
        if c == '\n': line += 1
        i += 1
    if quote or depth: raise RunnerError(f'Linha {line}: aspas/comentário sem fechamento.')
    if ''.join(buf).strip(): raise RunnerError(f'Linha {start}: faltou ponto e vírgula final.')


def classify(sql, line):
    # Não tratar palavras dentro de strings/identificadores como comandos.
    masked = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", 'IDENT', sql)
    if re.search(r'\b(?:CONCURRENTLY|CASCADE|TEMP(?:ORARY)?|UNLOGGED)\b', masked, re.I):
        raise RunnerError(f'Linha {line}: operação especial não suportada: {sql[:120]}')
    if re.search(r'(?i)\b_pgddl_runner\b', sql):
        raise RunnerError(f'Linha {line}: schema de controle reservado.')
    for kind, pattern in RULES:
        if re.match(pattern, sql, re.I):
            # Evita CREATE TABLE AS / SELECT e comandos ALTER TABLE compostos.
            if kind == 'CREATE TABLE' and not re.match(r'(?is)^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\w.\"]+\s*\(', sql):
                break
            if kind.startswith('ALTER TABLE'):
                # Rejeita vírgula de nível zero (múltiplas ações no mesmo ALTER).
                nesting = 0
                for c in masked:
                    if c == '(': nesting += 1
                    elif c == ')': nesting -= 1
                    elif c == ',' and nesting == 0:
                        raise RunnerError(f'Linha {line}: ALTER TABLE composto não suportado.')
            # IF [NOT] EXISTS pode encobrir divergência de estrutura.
            if re.search(r'\bIF\s+(?:NOT\s+)?EXISTS\b', masked, re.I):
                raise RunnerError(f'Linha {line}: IF [NOT] EXISTS exige revisão; não permitido na versão 0.4.')
            return kind
    raise RunnerError(f'Linha {line}: comando fora do escopo 0.4: {sql[:150]}')


def load_manifest(path, encoding='auto'):
    p = Path(path)
    raw = p.read_bytes()
    if encoding == 'auto':
        try: source = raw.decode('utf-8-sig'); encoding = 'utf-8-sig'
        except UnicodeDecodeError: source = raw.decode('cp1252'); encoding = 'cp1252'
    else: source = raw.decode(encoding)
    if '\x00' in source: raise RunnerError('Arquivo contém NUL.')
    steps = tuple(Step(n, line, sql, classify(sql, line), hashlib.sha256(sql.encode()).hexdigest())
                  for n, (line, sql) in enumerate(split_sql(source), 1))
    if not steps: raise RunnerError('Arquivo sem comandos SQL.')
    return Manifest(p, encoding, hashlib.sha256(raw).hexdigest(), steps)


def preview(s):
    return ' '.join(s.sql.split())[:180]


def plan(m, full=False):
    print(f'PG DDL Runner {VERSION} | {m.path.name}\nCodificação: {m.encoding}\nSHA-256: {m.digest}\nComandos: {len(m.steps)}')
    for kind, n in collections.Counter(s.kind for s in m.steps).most_common():
        print(f'  {n:5d}  {kind}')
    print('ATENÇÃO: RESTART de sequences, ALTER TYPE e índices podem causar impacto operacional.')
    if full:
        for s in m.steps: print(f'[{s.number:04d}] linha {s.line}: {preview(s)}')


def setup_logging(path):
    LOG.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S')
    screen = logging.StreamHandler(sys.stdout); screen.setFormatter(formatter); LOG.addHandler(screen)
    if path:
        # Dados de log podem conter nomes/SQL do fornecedor; não usar caminho público.
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
        handler = logging.FileHandler(path, encoding='utf-8'); handler.setFormatter(formatter); LOG.addHandler(handler)


def init_history(conn):
    with conn.transaction():
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS {HISTORY}')
        conn.execute(f'''CREATE TABLE IF NOT EXISTS {HISTORY}.migrations (
            target_schema text NOT NULL, migration_id text NOT NULL, file_sha256 text NOT NULL,
            total integer NOT NULL, encoding text NOT NULL, db_role text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (target_schema, migration_id), UNIQUE (target_schema, file_sha256))''')
        conn.execute(f'''CREATE TABLE IF NOT EXISTS {HISTORY}.applied (
            target_schema text NOT NULL, migration_id text NOT NULL, step integer NOT NULL,
            step_sha256 text NOT NULL, source_line integer NOT NULL, kind text NOT NULL,
            mode text NOT NULL CHECK (mode IN ('executed','acknowledged')),
            elapsed_ms bigint NOT NULL, detail jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (target_schema, migration_id, step),
            FOREIGN KEY (target_schema, migration_id) REFERENCES {HISTORY}.migrations(target_schema, migration_id))''')
        conn.execute(f'''CREATE TABLE IF NOT EXISTS {HISTORY}.errors (
            target_schema text NOT NULL, migration_id text NOT NULL, step integer NOT NULL,
            sqlstate text, message text NOT NULL, occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            FOREIGN KEY (target_schema, migration_id) REFERENCES {HISTORY}.migrations(target_schema, migration_id))''')


def read_state(conn, key, m, role, create=False):
    """Returns number of contiguous, hash-verified committed steps, or None."""
    if not conn.execute('SELECT to_regclass(%s)', (f'{HISTORY}.migrations',)).fetchone()[0]:
        return None
    row = conn.execute(f'SELECT file_sha256,total,encoding,db_role FROM {HISTORY}.migrations WHERE target_schema=%s AND migration_id=%s', key).fetchone()
    if row is None:
        other = conn.execute(f'SELECT migration_id FROM {HISTORY}.migrations WHERE target_schema=%s AND file_sha256=%s', (key[0], m.digest)).fetchone()
        if other: raise RunnerError(f'Arquivo já registrado como {other[0]!r}; use o ID original.')
        if not create: return None
        with conn.transaction():
            conn.execute(f'INSERT INTO {HISTORY}.migrations(target_schema,migration_id,file_sha256,total,encoding,db_role) VALUES (%s,%s,%s,%s,%s,%s)', (*key, m.digest, len(m.steps), m.encoding, role))
        return 0
    if tuple(row) != (m.digest, len(m.steps), m.encoding, role):
        raise RunnerError('Arquivo, quantidade, codificação ou usuário diverge do registro original; retomada bloqueada.')
    rows = conn.execute(f'SELECT step,step_sha256,source_line FROM {HISTORY}.applied WHERE target_schema=%s AND migration_id=%s ORDER BY step', key).fetchall()
    for i, (num, digest, line) in enumerate(rows, 1):
        if i > len(m.steps) or (num, digest, line) != (i, m.steps[i-1].digest, m.steps[i-1].line):
            raise RunnerError('Histórico inconsistente: lacuna, ordem ou hash inválido. Não prossiga.')
    return len(rows)


def record(conn, key, s, mode, ms, detail):
    conn.execute(f'''INSERT INTO {HISTORY}.applied(target_schema,migration_id,step,step_sha256,source_line,kind,mode,elapsed_ms,detail)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)''', (*key,s.number,s.digest,s.line,s.kind,mode,ms,json.dumps(detail,ensure_ascii=False)))



# Identificadores simples/qualificados, incluindo aspas. Não interpolar SQL livre.
IDENT = r'(?:"(?:[^"]|"")+"|[\w]+)'
REL = IDENT + r'(?:\s*\.\s*' + IDENT + r')?'
ALL_MODES = frozenset(('AccessShareLock','RowShareLock','RowExclusiveLock',
    'ShareUpdateExclusiveLock','ShareLock','ShareRowExclusiveLock','ExclusiveLock','AccessExclusiveLock'))
SHARE_CONFLICTS = frozenset(('RowExclusiveLock','ShareUpdateExclusiveLock','ShareRowExclusiveLock','ExclusiveLock','AccessExclusiveLock'))
CONFLICTS = {'ACCESS EXCLUSIVE': ALL_MODES, 'SHARE': SHARE_CONFLICTS,
             'SHARE ROW EXCLUSIVE': SHARE_CONFLICTS | {'ShareLock'}}

@dataclasses.dataclass(frozen=True)
class Target:
    name: str
    mode: str = ''
    only: bool = False
    creates: bool = False


def target(s):
    if s.kind.startswith('ALTER TABLE'):
        pattern = rf'^ALTER\s+TABLE\s+(?P<only>ONLY\s+)?(?P<name>{REL})(?=\s|\()'
        mode = 'ACCESS EXCLUSIVE'
    elif s.kind in ('CREATE INDEX','CREATE UNIQUE INDEX'):
        pattern = rf'^CREATE\s+(?:UNIQUE\s+)?INDEX\s+{IDENT}\s+ON\s+(?P<only>ONLY\s+)?(?P<name>{REL})(?=\s|\()'
        mode = 'SHARE'
    elif s.kind == 'ALTER SEQUENCE RESTART':
        pattern = rf'^ALTER\s+SEQUENCE\s+(?P<name>{REL})(?=\s|$)'
        mode = 'SHARE ROW EXCLUSIVE'
    elif s.kind in ('CREATE TABLE','CREATE SEQUENCE'):
        pattern = rf'^CREATE\s+(?:TABLE|SEQUENCE)\s+(?P<name>{REL})(?=\s|\(|$)'
        mode = ''
    else:
        raise RunnerError(f'Linha {s.line}: alvo não suportado.')
    match = re.match(pattern, s.sql, re.I)
    if not match:
        raise RunnerError(f'Linha {s.line}: não foi possível identificar o alvo com segurança.')
    return Target(match['name'], mode, bool(match.groupdict().get('only')), not mode)


def canonical_name(name, schema):
    # Somente para reconhecer objetos criados em passos anteriores no dry run.
    parts = re.findall(IDENT, name)
    parts = [v[1:-1].replace('""','"') if v.startswith('"') else v.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')) for v in parts]
    return tuple(parts if len(parts)==2 else [schema,*parts])


def relation_info(conn, t):
    return conn.execute('SELECT oid, relkind FROM pg_catalog.pg_class WHERE oid=pg_catalog.to_regclass(%s)', (t.name,)).fetchone()


def recursive_target(t, info):
    return not t.only and (t.mode == 'ACCESS EXCLUSIVE' or (t.mode == 'SHARE' and info and info[1]=='p'))


def lock_snapshot(conn, t, info):
    # Fotografia de possíveis conflitos, não teste de aquisição.
    if not info or not t.mode:
        return []
    return conn.execute('''WITH RECURSIVE rels(oid) AS (
        SELECT %s::oid UNION SELECT i.inhrelid FROM pg_catalog.pg_inherits i
        JOIN rels r ON i.inhparent=r.oid WHERE %s::boolean
      ) SELECT l.relation::regclass::text,l.pid,l.mode,l.granted,
        a.usename,a.application_name,a.client_addr::text,a.state,
        round(extract(epoch FROM clock_timestamp()-a.xact_start))::bigint,
        a.wait_event_type,a.wait_event
      FROM pg_catalog.pg_locks l JOIN rels r ON r.oid=l.relation
      LEFT JOIN pg_catalog.pg_stat_activity a ON a.pid=l.pid
      WHERE l.locktype='relation' AND l.database=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
        AND l.pid IS DISTINCT FROM pg_backend_pid() AND l.mode=ANY(%s::text[])
      ORDER BY l.granted DESC,l.pid,l.relation''', (info[0],bool(recursive_target(t,info)),sorted(CONFLICTS[t.mode]))).fetchall()


def log_conflicts(rows):
    for rel,pid,mode,granted,user,app,ip,state,age,wt,we in rows:
        LOG.warning('CONFLITO POTENCIAL tabela=%s PID=%s lock=%s (%s) usuário=%s app=%s IP=%s estado=%s transação=%ss espera=%s/%s',
            rel,pid if pid is not None else 'PREPARED XACT',mode,'concedido' if granted else 'na fila',
            user or '-',app or '-',ip or '-',state or '-',age if age is not None else '?',wt or '-',we or '-')


def identifier_value(name):
    return canonical_name(name, '')[-1]


def simple_type(text):
    # Normalize only known built-in spellings; never infer full equivalence.
    text = re.sub(r'\s+', ' ', text.strip().lower())
    text = re.sub(r'\s*([(),])\s*', r'\1', text)
    text = re.sub(r'^varchar(?=\(|$)', 'character varying', text)
    text = re.sub(r'^decimal(?=\(|$)', 'numeric', text)
    text = {'int':'integer','int4':'integer','int8':'bigint','int2':'smallint','bool':'boolean'}.get(text,text)
    text = re.sub(r'^numeric\((\d+)\)$', r'numeric(\1,0)', text)
    if re.fullmatch(r'(?:character varying(?:\(\d+\))?|numeric(?:\(\d+,-?\d+\))?|integer|bigint|smallint|boolean|text|date|timestamp(?:\(\d+\))? (?:with|without) time zone)',text):
        return text
    return None


def column_request(s):
    if s.kind not in ('ALTER TABLE ADD COLUMN','ALTER TABLE ALTER COLUMN TYPE'):
        return None
    action = r'ADD\s+(?:COLUMN\s+)?' if s.kind.endswith('ADD COLUMN') else r'ALTER\s+(?:COLUMN\s+)?'
    match = re.match(rf'^ALTER\s+TABLE\s+(?:ONLY\s+)?{REL}\s+{action}(?P<col>{IDENT})\s+(?P<tail>.+)$',s.sql,re.I|re.S)
    if not match: return None
    tail = match['tail']
    if s.kind.endswith('TYPE'):
        tail = re.sub(r'^(?:SET\s+DATA\s+)?TYPE\s+', '', tail, flags=re.I)
    # For defaults, constraints, COLLATE, USING etc the complete type is unknown.
    return identifier_value(match['col']), simple_type(tail)


def index_request(s):
    if s.kind not in ('CREATE INDEX','CREATE UNIQUE INDEX'): return None
    match = re.match(rf'^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?P<idx>{IDENT})\s+ON\s+(?:ONLY\s+)?{REL}\s*(?:USING\s+btree\s*)?\((?P<cols>.*)\)\s*$',s.sql,re.I|re.S)
    if not match: return None
    parts = [v.strip() for v in match['cols'].split(',')]
    if not all(re.fullmatch(IDENT,v) for v in parts): return None
    return identifier_value(match['idx']), tuple(identifier_value(v) for v in parts), s.kind=='CREATE UNIQUE INDEX'


def structural_snapshot(conn, oid):
    # Catalog-only estimates: no table scans, no pg_total_relation_size locks.
    meta = conn.execute("SELECT relpages, reltuples, current_setting('block_size')::bigint FROM pg_catalog.pg_class WHERE oid=%s",(oid,)).fetchone()
    cols = conn.execute('''SELECT attname, pg_catalog.format_type(atttypid,atttypmod), attnotnull, atthasdef
        FROM pg_catalog.pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped''',(oid,)).fetchall()
    indexes = conn.execute('''SELECT ic.relname, i.indisunique, i.indisvalid AND i.indisready,
        am.amname, i.indpred IS NULL AND i.indexprs IS NULL AND i.indnatts=i.indnkeyatts
            AND NOT EXISTS (SELECT 1 FROM unnest(i.indoption::smallint[]) AS o(opt) WHERE o.opt<>0),
        ARRAY(SELECT a.attname FROM generate_series(0,i.indnkeyatts::integer-1) AS g(k)
              LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[g.k]
              ORDER BY g.k)
        FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class ic ON ic.oid=i.indexrelid
        JOIN pg_catalog.pg_am am ON am.oid=ic.relam WHERE i.indrelid=%s ORDER BY ic.relname''',(oid,)).fetchall()
    return meta, {c[0]:c[1:] for c in cols}, indexes


def structural_findings(s, conn, info, cache, planned_cols, planned_indexes, name, planned):
    # Advisory only: catalog state plus explicitly tracked earlier DDL.
    t = target(s)
    findings = []
    meta, cols, indexes = None, {}, []
    existing = bool(info and info[1] in ('r','p','m','f'))
    if existing:
        if info[0] not in cache: cache[info[0]] = structural_snapshot(conn,info[0])
        meta, cols, indexes = cache[info[0]]
    known_table = existing or name in planned
    col = column_request(s)
    idx = index_request(s)
    if col and known_table:
        cname, wanted = col
        previous = planned_cols.get((name,cname))
        current = cols.get(cname)
        if s.kind.endswith('ADD COLUMN'):
            if current:
                typ, notnull, default = current
                same = wanted is not None and simple_type(typ)==wanted
                findings.append(('COLUNA_EXISTENTE',f'{cname}: atual={typ}, solicitado={wanted or "não interpretado"}, tipo={"compatível" if same else "divergente ou não comparado"}, not_null={notnull}, default={default}; não comprova equivalência',True))
            elif (name,cname) in planned_cols:
                findings.append(('COLUNA_REPETIDA_NO_ARQUIVO',cname,True))
            planned_cols[(name,cname)] = wanted
        else:
            actual = previous if (name,cname) in planned_cols else (current[0] if current else None)
            if not current and (name,cname) not in planned_cols and existing:
                findings.append(('COLUNA_ALVO_AUSENTE',cname,True))
            findings.append(('ALTER_TYPE_REVISAR',f'{cname}: {actual or "desconhecido"} -> {wanted or "não interpretado"}; pode reescrever tabela e reconstruir índices',False))
            planned_cols[(name,cname)] = wanted
    if idx and known_table:
        iname, keys, unique = idx
        if existing:
            absent = [k for k in keys if k not in cols and (name,k) not in planned_cols]
            if absent: findings.append(('COLUNA_INDICE_AUSENTE',', '.join(absent),True))
            collision = conn.execute('''SELECT c.relkind FROM pg_catalog.pg_class c
                WHERE c.relnamespace=(SELECT relnamespace FROM pg_catalog.pg_class WHERE oid=%s) AND c.relname=%s''',(info[0],iname)).fetchone()
            if collision:
                findings.append(('NOME_INDICE_EXISTENTE',f'{iname}: relkind={collision[0]}',True))
        ikey=(name[0],iname)
        if ikey in planned_indexes:
            findings.append(('NOME_INDICE_REPETIDO_NO_ARQUIVO',iname,True))
        for oldname, oldunique, valid, method, plain, oldkeys in indexes:
            if not (valid and method=='btree' and plain and oldunique==unique): continue
            if tuple(oldkeys)==keys:
                findings.append(('INDICE_CANDIDATO_REDUNDANTE',f'{oldname} tem mesmas chaves/ordem e unicidade de {iname}; revisar detalhes, não pular',True))
        for (namespace, oldname), (oldtable, oldkeys, oldunique) in planned_indexes.items():
            if oldtable==name and oldkeys==keys and oldunique==unique and oldname!=iname:
                findings.append(('INDICE_CANDIDATO_NO_ARQUIVO',f'{oldname} e {iname}: mesmas chaves/ordem e unicidade',True))
        planned_indexes[ikey]=(name,keys,unique)
        if unique: findings.append(('INDICE_UNICO_REVISAR','Pode falhar por valores duplicados; dry run não lê dados.',False))
    elif s.kind in ('CREATE INDEX','CREATE UNIQUE INDEX'):
        findings.append(('INDICE_NAO_COMPARADO','Forma complexa ou tabela ausente; verificar manualmente.',False))
    if s.kind=='ALTER SEQUENCE RESTART' and info:
        findings.append(('SEQUENCE_EXISTENTE_REVISAR','RESTART pode reutilizar valores; conferir antes de aplicar.',False))
    if meta and s.kind in ('CREATE INDEX','CREATE UNIQUE INDEX','ALTER TABLE ALTER COLUMN TYPE'):
        pages, tuples, block = meta
        findings.append(('IMPACTO_ESTIMADO',f'heap por catálogo={pages*block/1048576:.1f} MiB; linhas estimadas={tuples:.0f}; não inclui índices/TOAST/partições e pode estar desatualizado',False))
    return findings


def retry_prelock(error, phase, policy, deadline, interval, now):
    # Only a failed explicit prelock can retry, before DDL/checkpoint/commit.
    if policy!='retry' or phase!='prelock' or getattr(error,'sqlstate',None)!='55P03': return None
    remaining = deadline-now
    return min(interval,remaining) if remaining>0 else None


def dependency_graph(conn, s):
    if s.kind != 'ALTER TABLE ALTER COLUMN TYPE': return [], [], False
    t = target(s)
    col = column_request(s)
    info = relation_info(conn,t)
    if not col or not info: return [], [], False
    nodes, edges = viewdeps.discover(conn,info[0],col[0],t.only)
    hierarchy = info[1]!='r' or conn.execute('SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_inherits WHERE inhparent=%s OR inhrelid=%s)',(info[0],info[0])).fetchone()[0]
    return nodes,edges,hierarchy


def dry_run(conn, args, m, key, role):
    n = read_state(conn,key,m,role) or 0
    LOG.info('DRY RUN: análise de catálogo somente leitura; %d/%d passos confirmados.',n,len(m.steps))
    cache, planned, counts, reported = {}, set(), collections.Counter(), set()
    structure, planned_cols, planned_indexes, finding_counts = {}, {}, {}, collections.Counter()
    structural = not getattr(args,"locks_only",False)
    structural_alerts = 0
    pending = m.steps[n:]
    if args.max_steps: pending = pending[:args.max_steps]
    for s in pending:
        t = target(s)
        name = canonical_name(t.name,args.schema)
        cachekey = (t.name,t.mode,t.only)
        if cachekey not in cache:
            info = relation_info(conn,t)
            cache[cachekey] = (info,lock_snapshot(conn,t,info))
        info,rows = cache[cachekey]
        if t.creates:
            state = 'EXISTENTE' if info or name in planned else 'CRIACAO_PREVISTA'
            planned.add(name)
        elif info is None:
            state = 'DEPENDENCIA_PREVISTA' if name in planned else 'ALVO_AUSENTE'
        elif (s.kind=='ALTER SEQUENCE RESTART' and info[1]!='S') or (s.kind!='ALTER SEQUENCE RESTART' and info[1] not in ('r','p','m','f')):
            state = 'TIPO_INESPERADO'
        elif rows:
            state = 'LOCK_POTENCIAL'
        else:
            state = 'SEM_CONFLITO_VISIVEL'
        counts[state] += 1
        if structural:
            if s.kind == 'ALTER TABLE ALTER COLUMN TYPE':
                depnodes, _, _ = dependency_graph(conn,s)
                if depnodes:
                    viewdeps.report(depnodes,s.number)
                    finding_counts['VIEW_DEPENDENTE'] += sum(x['kind']=='v' for x in depnodes)
                    finding_counts['MATERIALIZED_VIEW_DEPENDENTE'] += sum(x['kind']=='m' for x in depnodes)
                    structural_alerts += 1
            for code, detail, alert in structural_findings(s,conn,info,structure,planned_cols,planned_indexes,name,planned):
                finding_counts[code] += 1
                structural_alerts += int(alert)
                if alert or args.verbose or code in ('ALTER_TYPE_REVISAR','INDICE_UNICO_REVISAR','SEQUENCE_EXISTENTE_REVISAR','IMPACTO_ESTIMADO'):
                    LOG.info('[%04d] %s %s | %s',s.number,code,t.name,detail)

        if args.verbose or state in ('EXISTENTE','ALVO_AUSENTE','TIPO_INESPERADO','LOCK_POTENCIAL'):
            LOG.info('[%04d/%04d] linha %d %s %s | %s',s.number,len(m.steps),s.line,state,t.mode or '-',preview(s))
        if rows and state=='LOCK_POTENCIAL' and cachekey not in reported:
            log_conflicts(rows); reported.add(cachekey)
    LOG.info('RESUMO DRY RUN: %d analisados | %s',len(pending),', '.join(f'{k}={v}' for k,v in sorted(counts.items())) or 'nenhum pendente')
    if structural: LOG.info('RESUMO ESTRUTURAL: %s', ', '.join(f'{k}={v}' for k,v in sorted(finding_counts.items())) or 'nenhum alerta adicional')
    LOG.info('Sem DDL, sem LOCK TABLE e sem escrita de histórico. Fotografia por alvo; não comprova sintaxe, permissões, dados, dependências completas ou disponibilidade futura. Comparação estrutural é indicativa.')
    return 4 if structural_alerts or any(counts[k] for k in ('EXISTENTE','ALVO_AUSENTE','TIPO_INESPERADO','LOCK_POTENCIAL')) else 0


def acquire_table(conn, t):
    from psycopg import sql
    if t.mode not in ('ACCESS EXCLUSIVE','SHARE'):
        return
    # Para ALTER normal inclui descendentes. Para índice comum, somente a tabela;
    # índices em tabela particionada incluem descendentes, salvo ON ONLY.
    info = relation_info(conn,t)
    if info and info[1] not in ('r','p','m'):
        # Deixa o próprio DDL validar objetos especiais; não presume LOCK TABLE válido.
        LOG.warning('Pré-lock NOWAIT não aplicado a relkind=%s alvo=%s; prevalece lock_timeout.',info[1],t.name)
        return
    parts = re.findall(IDENT,t.name)
    parts = [v[1:-1].replace('""','"') if v.startswith('"') else v.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')) for v in parts]
    conn.execute(sql.SQL('LOCK TABLE {}{} IN {} MODE NOWAIT').format(
        sql.SQL('' if recursive_target(t,info) else 'ONLY '),sql.Identifier(*parts),sql.SQL(t.mode)))


def monitor_backend(options, pid, stop, interval):
    # Conexão independente durante o DDL.
    import psycopg
    try:
        with psycopg.connect(autocommit=True,application_name='pgddl-monitor',connect_timeout=5,**options) as c:
            c.execute("SET statement_timeout='3s'")
            c.execute('SET default_transaction_read_only=on')
            while not stop.wait(interval):
                row=c.execute('''SELECT state,wait_event_type,wait_event,
                    round(extract(epoch FROM clock_timestamp()-query_start))::bigint,
                    CASE WHEN wait_event_type='Lock' THEN pg_blocking_pids(pid) ELSE ARRAY[]::integer[] END
                    FROM pg_catalog.pg_stat_activity WHERE pid=%s''',(pid,)).fetchone()
                if not row or row[0] not in ('active','idle in transaction (aborted)') or stop.is_set(): continue
                state,wt,we,age,blockers=row
                LOG.info('MONITOR PID=%s duração=%ss espera=%s/%s bloqueadores=%s',pid,age,wt or '-',we or '-',blockers)
                if blockers:
                    for b in c.execute('''SELECT pid,usename,application_name,client_addr::text,state,
                        round(extract(epoch FROM clock_timestamp()-xact_start))::bigint,wait_event_type,wait_event
                        FROM pg_catalog.pg_stat_activity WHERE pid=ANY(%s::integer[])''',(blockers,)).fetchall():
                        LOG.warning('BLOQUEADOR REAL PID=%s usuário=%s app=%s IP=%s estado=%s transação=%ss espera=%s/%s',*b)
                    if 0 in blockers: LOG.warning('BLOQUEADOR REAL: transação preparada (PID 0).')
                progress=c.execute('''SELECT phase,blocks_done,blocks_total,tuples_done,tuples_total
                    FROM pg_catalog.pg_stat_progress_create_index WHERE pid=%s''',(pid,)).fetchone()
                if progress: LOG.info('ÍNDICE fase=%s blocos=%s/%s tuplas=%s/%s',*progress)
    except Exception as exc:
        if not stop.is_set(): LOG.warning('Monitor indisponível; execução segue com os limites de lock/statement: %s',exc)


def db_run(args, m):
    try:
        import psycopg
        from psycopg import sql
    except ImportError as e:
        raise RunnerError('psycopg não instalado. Execute install.sh.') from e
    options = {k: getattr(args,k) for k in ('dbname','host','port','user') if getattr(args,k) is not None}
    conn = psycopg.connect(autocommit=True, application_name='pgddl-runner', **options)
    key = (args.schema, args.migration_id or m.path.name)
    stop_monitor = threading.Event()
    monitor = None
    messages = []
    def notice(diag):
        entry = {'severity':diag.severity_nonlocalized or diag.severity or 'NOTICE', 'message':diag.message_primary or ''}
        messages.append(entry)
        (LOG.warning if entry['severity'] in ('WARNING','ERROR','FATAL','PANIC') else LOG.info)('POSTGRES %s: %s', entry['severity'],entry['message'])
    conn.add_notice_handler(notice)
    try:
        if args.command in ('dry-run','status','dependencies','report','validate'):
            conn.execute('SET default_transaction_read_only=on')
        db, role, server, port, pid = conn.execute("SELECT current_database(), current_user, inet_server_addr(), current_setting('port'), pg_backend_pid()").fetchone()
        if db != args.dbname: raise RunnerError('Banco efetivo divergente do banco solicitado.')
        if args.schema == HISTORY or args.schema.startswith('pg_') or args.schema == 'information_schema':
            raise RunnerError('Schema alvo reservado.')
        exists = conn.execute('SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)', (args.schema,)).fetchone()[0]
        if not exists: raise RunnerError(f'Schema alvo inexistente: {args.schema}')
        conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(args.schema)))
        conn.execute('SET standard_conforming_strings = on')
        conn.execute("SELECT set_config('lock_timeout', %s, false), set_config('statement_timeout', %s, false)", (f'{args.lock_timeout}s',f'{args.statement_timeout}s'))
        effective = conn.execute('SELECT current_schema()').fetchone()[0]
        if effective != args.schema: raise RunnerError('Schema alvo não está acessível ao usuário.')
        LOG.info('Banco=%s servidor=%s:%s usuário=%s schema=%s PID=%s',db,server or 'socket local',port or '-',role,args.schema,pid)
        LOG.info('Migração=%s comandos=%s SHA256=%s',key[1],len(m.steps),m.digest)
        if args.command in ('apply','ack') and not conn.execute('SELECT pg_try_advisory_lock(%s::bigint)', (LOCK_KEY,)).fetchone()[0]:
            raise RunnerError('Outro executor está ativo neste banco. Nenhum DDL executado.')
        if args.command == 'dry-run':
            return dry_run(conn,args,m,key,role)
        if args.command == 'dependencies':
            if not 1 <= args.step <= len(m.steps): raise RunnerError('Passo fora do arquivo.')
            s=m.steps[args.step-1]
            if s.kind != 'ALTER TABLE ALTER COLUMN TYPE': raise RunnerError('dependencies suporta ALTER COLUMN TYPE.')
            with conn.transaction():
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                nodes,edges,hierarchy=dependency_graph(conn,s)
                if not nodes:
                    LOG.info('Nenhuma view dependente identificada no catálogo para passo %d.',s.number)
                    return 0
                args._hierarchy=hierarchy
                viewdeps.process(conn,nodes,edges,args,s,m,rebuild=False)
            return 4 if any(n['kind']=='m' for n in nodes) else 0
        if args.command in ('status','report','validate'):
            with conn.transaction():
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                n=read_state(conn,key,m,role)
                data=audit.collect(conn,key,m,n)
                summary=audit.summarize(data)
                validation=None
                if args.command=='validate' or getattr(args,'validate',False):
                    validation=audit.validate(conn,m,data['applied'],args.schema,target,canonical_name,column_request,index_request)
                identity={'Banco':db,'Servidor':str(server or 'socket local'),'Porta':port,'Schema':args.schema,'Usuário':role,'Migração':key[1],
                    'SHA-256':m.digest,'Comandos':len(m.steps),'Captura':datetime.datetime.now().astimezone().isoformat()}
            audit.status_log(LOG,data,summary,m,getattr(args,'history',False))
            if getattr(args,'migrations',False):
                for mid,total,confirmed,created,digest,ack in data['migrations']:
                    LOG.info('MIGRAÇÃO %s %d/%d ack=%d | %s | SHA256=%s',mid,confirmed,total,ack,created,digest)
            if validation is not None:
                for v in validation:
                    if v['status']!='OK' or getattr(args,'verbose',False):
                        (LOG.warning if v['status']=='DIVERGÊNCIA' else LOG.info)('VALIDAÇÃO [%d] %s %s: %s',v['step'],v['status'],v['object'],v['message'])
                LOG.info('VALIDAÇÃO LIMITADA: %s',dict(collections.Counter(v['status'] for v in validation)))
                if n is None or n<len(m.steps): LOG.warning('Migração incompleta/não registrada: validação somente de passos confirmados.')
            if args.command=='report':
                path=audit.write_report(audit.render(data,summary,identity,validation),args.report_dir)
                LOG.info('RELATÓRIO HTML: %s',path)
            return 4 if validation is not None and any(v['status']=='DIVERGÊNCIA' for v in validation) else 0
        if args.command == 'apply':
            if not args.yes:
                if not sys.stdin.isatty(): raise RunnerError('Modo não interativo exige --yes.')
                if input(f'Aplicar em {db}/{args.schema}? Digite o nome do banco: ').strip() != db:
                    raise RunnerError('Confirmação incorreta; operação cancelada.')
            maintenance_settings=audit.configure(conn,args,LOG)
            init_history(conn)
            other = conn.execute(f'''SELECT m.migration_id FROM {HISTORY}.migrations m
                WHERE m.target_schema=%s AND m.migration_id<>%s AND m.total >
                  (SELECT count(*) FROM {HISTORY}.applied a WHERE a.target_schema=m.target_schema AND a.migration_id=m.migration_id)
                LIMIT 1''', key).fetchone()
            if other: raise RunnerError(f'Outra migração está incompleta neste schema: {other[0]!r}. Retome-a primeiro.')
        n = read_state(conn,key,m,role,create=args.command=='apply')
        if n is None: raise RunnerError('Migração não registrada. Use apply primeiro.')
        if args.command == 'ack':
            if n >= len(m.steps) or args.step != n+1: raise RunnerError('Só é permitido reconhecer o próximo comando pendente.')
            if not args.yes or not args.reason.strip(): raise RunnerError('ack exige --yes e --reason não vazia.')
            s = m.steps[n]
            LOG.warning('RECONHECIMENTO MANUAL [%d] linha %d: %s | motivo: %s',s.number,s.line,s.sql,args.reason)
            with conn.transaction(): record(conn,key,s,'acknowledged',0,{'reason':args.reason,'role':role})
            LOG.info('Reconhecimento registrado; nenhum DDL executado. Rode apply para continuar.')
            return 0
        if n == len(m.steps): LOG.info('Migração já concluída. Nenhum comando reexecutado.'); return 0
        LOG.info('Retomada: %d confirmados; começando pelo %d.',n,n+1)
        if args.monitor_interval:
            monitor = threading.Thread(target=monitor_backend,args=(options,pid,stop_monitor,args.monitor_interval),daemon=True)
            monitor.start()
        run_start = time.monotonic()
        for s in m.steps[n:]:
            messages.clear()
            LOG.info('[%04d/%04d] linha %d EXEC %s',s.number,len(m.steps),s.line,s.sql if args.verbose else preview(s))
            t = target(s)
            begin = time.monotonic()
            deadline = begin + args.retry_timeout
            attempt = 1
            while True:
                phase = 'begin'
                try:
                    # DDL e checkpoint têm o mesmo destino transacional.
                    with conn.transaction():
                        phase = 'prelock'
                        if args.lock_policy in ('nowait','retry') or (s.kind=='ALTER TABLE ALTER COLUMN TYPE' and args.dependency_policy=='rebuild-views'): acquire_table(conn,t)
                        phase = 'dependencies'
                        backup = None
                        if s.kind == 'ALTER TABLE ALTER COLUMN TYPE':
                            nodes,edges,hierarchy=dependency_graph(conn,s)
                            if nodes:
                                args._hierarchy=hierarchy
                                # Capture OID before temporarily changing search_path.
                                info=relation_info(conn,t)
                                col=column_request(s)[0]
                                args._rediscover=lambda: viewdeps.discover(conn,info[0],col,t.only)
                                backup=viewdeps.process(conn,nodes,edges,args,s,m,rebuild=args.dependency_policy=='rebuild-views')
                                if args.dependency_policy!='rebuild-views':
                                    raise viewdeps.DependencyError(f'Dependências de view detectadas; backup={backup}. Revise e autorize --dependency-policy rebuild-views se aplicável.')
                        if backup is None:
                            phase = 'ddl'
                            conn.execute(s.sql, prepare=True)
                        ms = round((time.monotonic()-begin)*1000)
                        phase = 'checkpoint'
                        record(conn,key,s,'executed',ms,{'notices':list(messages),'lock_attempts':attempt,'maintenance_settings':maintenance_settings, **({'view_backup':backup,'rebuilt_views':len(nodes),'view_names':[n['schema']+'.'+n['name'] for n in nodes]} if backup else {})})
                        phase = 'commit'
                    break
                except viewdeps.DependencyError as e:
                    LOG.error('[%04d] DEPENDENCY STOP: %s | transação atual revertida; checkpoint preservado.',s.number,e)
                    try:
                        with conn.transaction():
                            conn.execute(f'INSERT INTO {HISTORY}.errors(target_schema,migration_id,step,sqlstate,message) VALUES (%s,%s,%s,%s,%s)',(*key,s.number,'0A000',str(e)))
                    except Exception as audit_error:
                        LOG.error('Falha ao registrar diagnóstico: %s',audit_error)
                    return 2
                except psycopg.Error as e:
                    msg = str(e)
                    LOG.error('[%04d] fase=%s SQLSTATE=%s | %s\nSQL: %s',s.number,phase,e.sqlstate or '-',msg,s.sql)
                    if e.sqlstate in ('55P03','40P01'):
                        try:
                            rows=lock_snapshot(conn,t,relation_info(conn,t))
                            log_conflicts(rows)
                            if not rows: LOG.warning('Nenhum conflito ainda visível no alvo; pode ter desaparecido ou envolver outro objeto. Consulte MONITOR.')
                        except Exception as diag_error: LOG.warning('Diagnóstico pós-falha indisponível: %s',diag_error)
                    delay = retry_prelock(e,phase,args.lock_policy,deadline,args.retry_interval,time.monotonic())
                    if delay is not None and not conn.closed and not conn.broken and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
                        LOG.warning('[%04d] PRÉ-LOCK ocupado; tentativa=%d; nova tentativa em %.1fs, sem transação aberta.',s.number,attempt,delay)
                        time.sleep(delay)
                        if time.monotonic() < deadline:
                            attempt += 1
                            messages.clear()
                            continue
                    # Se conexão/COMMIT ficou incerto, não faça retry automático.
                    try:
                        if not conn.closed and not conn.broken:
                            with conn.transaction():
                                conn.execute(f'INSERT INTO {HISTORY}.errors(target_schema,migration_id,step,sqlstate,message) VALUES (%s,%s,%s,%s,%s)',(*key,s.number,e.sqlstate,msg))
                    except Exception as audit_error:
                        LOG.error('Não foi possível registrar falha no banco: %s',audit_error)
                    LOG.error('Execução interrompida. Corrija a causa e rode status/apply com o mesmo arquivo. Nenhuma nova tentativa automática deste comando.')
                    return 2
            elapsed = time.monotonic()-begin
            LOG.info('[%04d/%04d] OK %.2fs | %.1f%%',s.number,len(m.steps),elapsed,100*s.number/len(m.steps))
            if any(x['severity'] in ('WARNING','ERROR','FATAL','PANIC') for x in messages) and not args.continue_on_warning:
                LOG.warning('Pausa após WARNING: comando confirmado. Revise antes de retomar.')
                return 3
            if args.max_steps and s.number-n >= args.max_steps and s.number < len(m.steps):
                LOG.info('Pausa programada: %d comandos confirmados nesta execução.',s.number-n); return 0
        LOG.info('CONCLUÍDO: %d/%d | tempo desta execução %.2fs',len(m.steps),len(m.steps),time.monotonic()-run_start)
        return 0
    finally:
        stop_monitor.set()
        if monitor: monitor.join(timeout=1)
        conn.close()  # libera lock de sessão; transação não confirmada é descartada


class PortugueseParser(argparse.ArgumentParser):
    """Localiza a apresentação da ajuda sem alterar a interpretação da CLI."""
    def format_help(self):
        result = super().format_help()
        result = re.sub(r'^usage: ', 'Uso: ', result, flags=re.MULTILINE)
        result = re.sub(r'^positional arguments:$', 'Argumentos posicionais:', result, flags=re.MULTILINE)
        result = re.sub(r'^optional arguments:$|^options:$', 'Opções:', result, flags=re.MULTILINE)
        result = result.replace('show this help message and exit', 'Mostra esta ajuda e sai.').replace("show program's version number and exit", 'Mostra a versão e sai.')
        return result


def build_parser():
    # Os nomes, aliases, tipos, escolhas e defaults permanecem iguais aos da 0.5.0.
    commands = {
        'plan': ('Analisa o arquivo DDL sem conexão ao banco.', 'Lista famílias de comandos e SHA-256; não valida toda a sintaxe SQL.\nExemplo: ./pgddl plan atualizacao.ddl --full'),
        'dry-run': ('Analisa locks, estrutura e dependências no banco (somente leitura).', 'Não aplica DDL nem cria histórico. Os locks observados podem mudar.\nExemplo: ./pgddl dry-run atualizacao.ddl -d erp -U postgres -p 15777 --verbose'),
        'status': ('Consulta o progresso, checkpoints e erros pendentes ou resolvidos.', 'Somente leitura no banco. Pode ser usado durante apply.\nExemplo: ./pgddl status atualizacao.ddl -d erp -U postgres -p 15777 --history --migrations'),
        'apply': ('Aplica ou retoma o DDL na ordem original, com checkpoint por comando.', 'ALTERA O BANCO. Revise o SQL, faça backup e teste em clone.\nRetome com o mesmo arquivo, schema, papel e ID; não edite o arquivo.\nExemplos:\n  ./pgddl apply atualizacao.ddl -d erp -U postgres -p 15777 --max-steps 20\n  ./pgddl apply atualizacao.ddl -d erp -U postgres -p 15777 --lock-policy retry\n  ./pgddl apply atualizacao.ddl -d erp -U postgres -p 15777 --dependency-policy rebuild-views\nMaterialized views bloqueiam a reconstrução automática, mesmo quando autorizada.'),
        'ack': ('Reconhece manualmente o próximo passo pendente, sem executar seu SQL.', 'ALTERA O HISTÓRICO. Só use após verificar o efeito exato do DDL.\nExige --step, --reason e --yes. Não valida equivalência automaticamente.\nExemplo (número ilustrativo):\n  ./pgddl ack atualizacao.ddl -d erp -U postgres -p 15777 --step 20 --reason "Efeito verificado" --yes'),
        'dependencies': ('Identifica views dependentes de um ALTER COLUMN TYPE e exporta backup.', 'Somente leitura no banco; grava arquivos locais. Não reconstrói views.\nMATERIALIZED VIEW recebe destaque; backup é estrutural, não dos dados.\nExemplo: ./pgddl dependencies atualizacao.ddl -d erp -U postgres -p 15777 --step 20'),
        'report': ('Gera relatório HTML de execução, tempos, erros e histórico.', 'Somente leitura no banco; grava HTML local com metadados e SQL.\nExemplo: ./pgddl report atualizacao.ddl -d erp -U postgres -p 15777 --validate'),
        'validate': ('Confere propriedades estruturais simples dos passos confirmados.', 'Somente leitura. Não certifica dados, permissões ou equivalência de views.\nExemplo: ./pgddl validate atualizacao.ddl -d erp -U postgres -p 15777 --verbose'),
    }
    parser = PortugueseParser(prog='pgddl', description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Fluxo recomendado (teste em clone):
  ./pgddl plan atualizacao.ddl
  ./pgddl dry-run atualizacao.ddl -d erp -U postgres -p 15777
  ./pgddl apply atualizacao.ddl -d erp -U postgres -p 15777 --max-steps 20
  ./pgddl status atualizacao.ddl -d erp -U postgres -p 15777
  ./pgddl report atualizacao.ddl -d erp -U postgres -p 15777 --validate

Para ver opções, valores padrão e exemplos de um comando:
  ./pgddl apply -h
  ./pgddl <comando> -h

Windows: use .\\pgddl.cmd no lugar de ./pgddl; conexão via TCP
normalmente exige -H HOST e credenciais libpq/pgpass.conf.

Códigos de saída:
  0   Sucesso ou pausa programada; consulte status.
  1   Falha de preparação, controle ou conexão (fora do DDL).
  2   Erro de argumentos (CLI) ou falha durante DDL/checkpoint.
  3   Pausa após WARNING; passo já confirmado.
  4   Alertas no dry-run, materialized view em dependencies ou divergência na validação.
  130 Interrupção por Ctrl+C; consulte status antes de retomar.

Segurança: apply modifica o banco; ack modifica o histórico. Não há rollback
global. Use o mesmo arquivo original na retomada e preserve o histórico.
""")
    parser.add_argument('--version',action='version',version=VERSION,help='Mostra a versão e sai.')
    sub = parser.add_subparsers(dest='command',required=True,title='Comandos disponíveis',metavar='COMANDO')
    for cmd, (description, example) in commands.items():
        p = sub.add_parser(cmd, help=description, description=description,
                           epilog=example, formatter_class=argparse.RawDescriptionHelpFormatter)
        p.add_argument('file',type=Path,metavar='ARQUIVO.ddl',help='Arquivo DDL original da desenvolvedora.')
        p.add_argument('--encoding',default='auto',choices=['auto','utf-8','utf-8-sig','cp1252','latin-1'],help='Codificação do arquivo (padrão: auto).')
        if cmd=='plan':
            p.add_argument('--full',action='store_true',help='Lista uma prévia de cada comando (até 180 caracteres), além do resumo.')
            continue
        g = p.add_argument_group('Conexão e identidade da migração')
        g.add_argument('-d','--dbname',required=True,metavar='BANCO',help='Nome literal do banco, obrigatório (não URI/conninfo).')
        g.add_argument('-H','--host',metavar='HOST',help='Host ou diretório do socket; quando omitido, usa padrão libpq/PGHOST.')
        g.add_argument('-p','--port',type=int,metavar='PORTA',help='Porta PostgreSQL; quando omitida, usa padrão libpq/PGPORT.')
        g.add_argument('-U','--user',metavar='USUARIO',help='Papel PostgreSQL; quando omitido, usa padrão libpq/PGUSER.')
        g.add_argument('--schema',default='public',metavar='SCHEMA',help='Schema alvo e search_path (padrão: public).')
        g.add_argument('--migration-id',metavar='ID',help='ID estável da atualização (padrão: nome do arquivo). Preserve na retomada.')
        g = p.add_argument_group('Timeouts e log')
        g.add_argument('--lock-timeout',type=int,default=3,metavar='SEG',help='Limite por aquisição de lock (padrão: 3 s); 0 sem limite, não recomendado.')
        g.add_argument('--statement-timeout',type=int,default=0,metavar='SEG',help='Limite por instrução (padrão: 0, sem limite).')
        g.add_argument('--log',default=None,metavar='ARQUIVO',help='Log local (Windows: %%LOCALAPPDATA%%\\PGDDL\\logs; Linux: ~/.local/state/pgddl/logs; PGDDL_STATE_HOME personaliza).')
        g = p.add_argument_group('Opções de '+cmd)
        if cmd=='status':
            g.add_argument('--history',action='store_true',help='Exibe todas as falhas, inclusive resolvidas e reconhecidas.')
            g.add_argument('--migrations',action='store_true',help='Lista as migrações do banco/schema selecionado.')
        if cmd=='report':
            g.add_argument('--report-dir',type=Path,metavar='DIRETORIO',help='Destino HTML (Windows: LOCALAPPDATA/PGDDL/reports; Linux: ~/.local/state/pgddl/reports).')
            g.add_argument('--validate',action='store_true',help='Inclui verificação estrutural limitada no relatório.')
        if cmd=='validate': g.add_argument('--verbose',action='store_true',help='Mostra também as verificações com resultado OK.')
        if cmd=='dry-run':
            g.add_argument('--verbose',action='store_true',help='Mostra todos os passos analisados, não somente alertas/resumo.')
            g.add_argument('--locks-only',action='store_true',help='Analisa locks sem a análise estrutural e de dependências adicional.')
            g.add_argument('--max-steps',type=int,default=0,metavar='N',help='Limita passos pendentes analisados (padrão: 0, todos).')
        if cmd in ('apply','dependencies'):
            g.add_argument('--backup-dir',type=Path,metavar='DIRETORIO',help='Histórico das views (Windows: LOCALAPPDATA/PGDDL/views; Linux: ~/.local/state/pgddl/views).')
        if cmd=='dependencies': g.add_argument('--step',type=int,required=True,metavar='N',help='Número do passo ALTER COLUMN TYPE (obrigatório, começa em 1).')
        if cmd=='apply':
            g.add_argument('--dependency-policy',choices=['stop','rebuild-views'],default='stop',help='stop: exporta e pausa (padrão); rebuild-views: reconstrói apenas views comuns suportadas, em transação.')
            g.add_argument('--lock-policy',choices=['nowait','wait','retry'],default='nowait',help='nowait: pré-lock imediato (padrão); wait: fila com timeout; retry: repete pré-lock sem permanecer na fila.')
            g.add_argument('--retry-interval',type=int,default=5,metavar='SEG',help='Intervalo entre tentativas do pré-lock (padrão: 5 s, só retry).')
            g.add_argument('--retry-timeout',type=int,default=300,metavar='SEG',help='Janela de novas tentativas por passo (padrão: 300 s, só retry).')
            g.add_argument('--monitor-interval',type=int,default=2,metavar='SEG',help='Intervalo do monitor de locks/índices (padrão: 2 s); 0 desativa.')
            g.add_argument('--yes',action='store_true',help='Dispensa confirmação do nome do banco, inclusive em automação. Não ignora erros.')
            g.add_argument('--verbose',action='store_true',help='Exibe SQL integral de cada passo durante a execução.')
            g.add_argument('--max-steps',type=int,default=0,metavar='N',help='Pausa após N novos comandos confirmados (padrão: 0, sem limite).')
            g.add_argument('--continue-on-warning',action='store_true',help='Continua após WARNING; por padrão pausa após confirmar o passo. Não ignora erros SQL.')
            g.add_argument('--parallel-maintenance-workers','--max-parallel-maintenance-workers',type=int,metavar='N',help='Limite de workers nativos de manutenção nesta sessão; 0 desativa. Omitir preserva configuração.')
            g.add_argument('--maintenance-work-mem',metavar='MEMORIA',help='Memória de manutenção da sessão, ex.: 512MB. Unidades kB, MB ou GB. Omitir preserva configuração.')
        if cmd=='ack':
            g.add_argument('--step',type=int,required=True,metavar='N',help='Número do próximo passo pendente, obrigatório.')
            g.add_argument('--reason',required=True,metavar='MOTIVO',help='Justificativa não vazia e evidência da verificação, obrigatória.')
            g.add_argument('--yes',action='store_true',help='Confirma expressamente o reconhecimento manual (obrigatório).')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        m = load_manifest(args.file,args.encoding)
        for step in m.steps: target(step)
        if args.command=='plan': plan(m,args.full); return 0
        if args.lock_timeout < 0 or args.statement_timeout < 0 or getattr(args,'max_steps',0)<0 or getattr(args,'monitor_interval',0)<0:
            raise RunnerError('Timeouts e max-steps devem ser >= 0.')
        if args.command=='apply' and (args.retry_interval <= 0 or args.retry_timeout <= 0):
            raise RunnerError('retry-interval e retry-timeout devem ser positivos.')
        if args.command=='apply':
            if args.parallel_maintenance_workers is not None and args.parallel_maintenance_workers<0: raise RunnerError('Workers devem ser >= 0.')
            if args.maintenance_work_mem is not None and not re.fullmatch(r'[1-9][0-9]*(?:kB|MB|GB)',args.maintenance_work_mem): raise RunnerError('Use memória positiva com unidade kB, MB ou GB (ex.: 512MB).')
        if '=' in args.dbname or args.dbname.startswith(('postgresql://','postgres://')):
            raise RunnerError('Use o nome literal do banco; configure conexão por PGHOST/PGPORT/PGUSER/.pgpass.')
        if not args.log:
            directory = platform_paths.state_root()/'logs'
            directory.mkdir(mode=0o700,parents=True,exist_ok=True)
            args.log = directory / (datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')+f'-{args.command}-{os.getpid()}.log')
        setup_logging(args.log)
        LOG.info('Log: %s',args.log)
        return db_run(args,m)
    except KeyboardInterrupt:
        print('\nInterrompido. Consulte status e retome com o mesmo arquivo; checkpoint do banco é a referência.',file=sys.stderr)
        return 130
    except Exception as e:
        # Não expõe traceback nem configurações de conexão deliberadamente.
        if LOG.handlers: LOG.error('%s: %s',type(e).__name__,e)
        else: print(f'ERRO: {e}',file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
