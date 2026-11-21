"""Testes destrutivos, SOMENTE em banco descartável pgddl_test com superusuário.
Usa PGHOST/PGPORT/PGUSER/PGPASSFILE conforme libpq. Preserva schemas para auditoria.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
import psycopg
from psycopg import sql

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--dbname',required=True)
p.add_argument('--disposable',action='store_true',required=True)
a=p.parse_args()
if a.dbname!='pgddl_test': p.error('Use somente pgddl_test descartável.')
schema='view_lab_'+uuid.uuid4().hex[:10]
other=schema+'_bi'
reader=schema+'_reader'
with psycopg.connect(dbname=a.dbname,autocommit=True) as conn, tempfile.TemporaryDirectory() as temp:
    assert conn.execute('SELECT rolsuper FROM pg_roles WHERE rolname=current_user').fetchone()[0]
    conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(other)))
    conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
    conn.execute('CREATE TABLE t(id int, c varchar(20))')
    conn.execute("INSERT INTO t VALUES (1,'abc')")
    conn.execute(sql.SQL('CREATE VIEW {} WITH (security_barrier=true) AS SELECT id,c FROM t').format(sql.Identifier(other,'itens')))
    # Regression 42501: owner has SELECT only on source, not write/lock privileges.
    conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(reader)))
    for ns in (schema,other):
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(ns),sql.Identifier(reader)))
    conn.execute(sql.SQL('GRANT SELECT ON t TO {}').format(sql.Identifier(reader)))
    conn.execute(sql.SQL('ALTER VIEW {} OWNER TO {}').format(sql.Identifier(other,'itens'),sql.Identifier(reader)))
    try:
        with conn.transaction():
            conn.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE NOWAIT').format(sql.Identifier(other,'itens')))
    except psycopg.errors.InsufficientPrivilege:
        print('PASS: reproduzido SQLSTATE 42501 no pré-lock recursivo antigo')
    else:
        raise AssertionError('O cenário de regressão não reproduziu a falha esperada')
    conn.execute(sql.SQL('CREATE VIEW v2 AS SELECT * FROM {}').format(sql.Identifier(other,'itens')))
    conn.execute('CREATE VIEW v3 AS SELECT c FROM t')
    conn.execute('CREATE VIEW v4 AS SELECT c FROM t')
    conn.execute('COMMENT ON VIEW v2 IS \'view comment\'')
    conn.execute("COMMENT ON COLUMN v2.c IS 'column comment'")
    conn.execute('GRANT SELECT ON v2 TO PUBLIC')
    conn.execute('GRANT UPDATE(c) ON v2 TO PUBLIC')
    f=Path(temp)/'views.ddl'; backups=Path(temp)/'backups'
    f.write_text('ALTER TABLE t ADD COLUMN x integer;\nALTER TABLE t ALTER COLUMN c TYPE varchar(40);\n')
    def run(cmd,*opts,expected=0):
        args=[sys.executable,str(Path(__file__).with_name('pgddl.py')),cmd,str(f),'-d',a.dbname,'--schema',schema,*opts]
        r=subprocess.run(args,check=False)
        assert r.returncode==expected,(args,r.returncode,expected)
    def n():
        return conn.execute('SELECT count(*) FROM _pgddl_runner.applied WHERE target_schema=%s AND migration_id=%s',(schema,f.name)).fetchone()[0]
    def typ():
        return conn.execute("SELECT format_type(atttypid,atttypmod) FROM pg_attribute WHERE attrelid='t'::regclass AND attname='c'").fetchone()[0]
    run('dry-run',expected=4)
    run('dependencies','--step','2','--backup-dir',str(backups))
    run('apply','--yes','--backup-dir',str(backups),expected=2)
    assert n()==1 and typ()=='character varying(20)'
    run('apply','--yes','--dependency-policy','rebuild-views','--backup-dir',str(backups))
    assert n()==2 and typ()=='character varying(40)'
    assert conn.execute('SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=to_regclass(%s)',(other+'.itens',)).fetchone()[0]==reader
    assert conn.execute("SELECT has_table_privilege(%s,'t','SELECT'),has_table_privilege(%s,'t','UPDATE'),has_table_privilege(%s,'t','DELETE'),has_table_privilege(%s,'t','TRUNCATE')",(reader,)*4).fetchone()==(True,False,False,False)
    assert conn.execute('SELECT * FROM v2').fetchall()==[(1,'abc')]
    assert conn.execute("SELECT obj_description('v2'::regclass,'pg_class'),col_description('v2'::regclass,2)").fetchone()==('view comment','column comment')
    assert conn.execute("SELECT EXISTS(SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) x WHERE c.oid='v2'::regclass AND x.grantee=0 AND x.privilege_type='SELECT'), EXISTS(SELECT 1 FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x WHERE a.attrelid='v2'::regclass AND a.attname='c' AND x.grantee=0 AND x.privilege_type='UPDATE')").fetchone()==(True,True)
    paths=list(backups.glob('*/restore-views.sql')); assert len(paths)==3
    run('apply','--yes','--dependency-policy','rebuild-views'); assert n()==2
    # Materialized view transitively depending on the target. No DROP, no checkpoint.
    conn.execute('CREATE MATERIALIZED VIEW mv AS SELECT c FROM v2')
    conn.execute('CREATE INDEX mv_idx ON mv(c)')
    f=Path(temp)/'matview.ddl'; f.write_text('ALTER TABLE t ALTER COLUMN c TYPE varchar(60);\n')
    run('dry-run',expected=4)
    run('dependencies','--step','1','--backup-dir',str(backups),expected=4)
    run('apply','--yes','--dependency-policy','rebuild-views','--backup-dir',str(backups),expected=2)
    assert n()==0 and typ()=='character varying(40)'
    assert conn.execute('SELECT * FROM mv').fetchall()==[('abc',)]
    assert conn.execute("SELECT to_regclass('mv_idx') IS NOT NULL").fetchone()[0]
    assert any(any(v['kind']=='m' and v['indexes'] for v in json.loads(p.read_text())['views']) for p in backups.glob('*/views.json'))
    print('PASS: rebuild transacional, retomada, grafo entre schemas, ACL e comentários; matview preservada e bloqueada.')
    print('Schemas e role de laboratório preservados:',schema,other,reader)
