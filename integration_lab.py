"""Teste destrutivo SOMENTE em banco descartável explicitamente indicado.
Uso: .venv/bin/python integration_lab.py --dbname pgddl_test --disposable
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import psycopg
from psycopg import sql

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--dbname',required=True)
p.add_argument('--disposable',action='store_true',required=True)
a=p.parse_args()
if a.dbname != 'pgddl_test': p.error('Por segurança, use banco descartável chamado pgddl_test.')
schema='ddl_lab_'+uuid.uuid4().hex[:12]
with psycopg.connect(dbname=a.dbname,autocommit=True) as conn, tempfile.TemporaryDirectory() as d:
    conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
    f=Path(d)/'lab.ddl'
    f.write_text('CREATE TABLE t (id integer);\nCREATE SEQUENCE s;\nALTER SEQUENCE s RESTART WITH 1;\nALTER TABLE t ADD COLUMN x integer;\nCREATE INDEX ix ON t(x);\nALTER TABLE missing ADD COLUMN y integer;\nCREATE TABLE done (id integer);\n')
    def run(cmd,*extra,expected=0):
        argv=[sys.executable,str(Path(__file__).with_name('pgddl.py')),cmd,str(f),'-d',a.dbname,'--schema',schema,*extra]
        r=subprocess.run(argv,check=False)
        if r.returncode != expected: raise AssertionError(f'{cmd}: esperado {expected}, obtido {r.returncode}')
    def n():
        return conn.execute('SELECT count(*) FROM _pgddl_runner.applied WHERE target_schema=%s AND migration_id=%s',(schema,f.name)).fetchone()[0]
    run('apply','--yes','--max-steps','3'); assert n()==3
    assert conn.execute("SELECT nextval('s'),nextval('s')").fetchone()==(1,2)
    run('apply','--yes',expected=2); assert n()==5
    assert conn.execute('SELECT count(*) FROM _pgddl_runner.errors WHERE target_schema=%s',(schema,)).fetchone()[0]==1
    conn.execute('CREATE TABLE missing(id integer)')
    run('apply','--yes'); assert n()==7
    run('apply','--yes'); assert n()==7
    assert conn.execute("SELECT nextval('s')").fetchone()[0]==3
    run('status','--history','--migrations')
    run('report','--report-dir',d,'--validate')
    report=list(Path(d).glob('*_pgddl.html')); assert len(report)==1
    assert 'CONCLUÍDA COM SUCESSO' in report[0].read_text() and 'RESOLVIDA' in report[0].read_text()
    run('validate')
    old=f.read_text(); f.write_text(old+'-- alterado\n'); run('apply','--yes',expected=1); f.write_text(old)
    f2=Path(d)/'ack.ddl'; f2.write_text('CREATE TABLE t (id integer);\nCREATE TABLE ack_done (id integer);\n')
    f=f2
    run('apply','--yes',expected=2)
    run('ack','--step','1','--reason','Tabela t existente verificada no laboratório','--yes')
    run('apply','--yes'); assert n()==2
    assert conn.execute('SELECT mode FROM _pgddl_runner.applied WHERE target_schema=%s AND migration_id=%s AND step=1',(schema,f.name)).fetchone()[0]=='acknowledged'
    # Locks: SELECT bloqueia ALTER, mas não CREATE INDEX; escrita bloqueia ambos.
    f=Path(d)/'locks.ddl'; f.write_text('ALTER TABLE t ADD COLUMN lock_test integer;\nCREATE INDEX ix_lock ON t(lock_test);\n')
    with psycopg.connect(dbname=a.dbname,autocommit=True) as locker:
        locker.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        with locker.transaction():
            locker.execute('SELECT * FROM t')
            run('dry-run',expected=4)
            assert conn.execute('SELECT count(*) FROM _pgddl_runner.migrations WHERE target_schema=%s AND migration_id=%s',(schema,f.name)).fetchone()[0]==0
            run('apply','--yes',expected=2); assert n()==0
        run('apply','--yes','--max-steps','1'); assert n()==1
        with locker.transaction():
            locker.execute('SELECT * FROM t')
            run('dry-run'); assert n()==1
        with locker.transaction():
            locker.execute('INSERT INTO t(id) VALUES (42)')
            run('dry-run',expected=4)
            run('apply','--yes',expected=2); assert n()==1
        with locker.transaction():
            locker.execute('SELECT * FROM t')
            run('apply','--yes'); assert n()==2
    # 0.3: estrutural, incluindo coluna futura para o índice, sem escrita.
    f=Path(d)/'structure.ddl'
    f.write_text('ALTER TABLE t ADD COLUMN x varchar(11);\nCREATE INDEX ix_equiv ON t(x);\n')
    run('dry-run',expected=4)
    assert conn.execute('SELECT count(*) FROM _pgddl_runner.migrations WHERE target_schema=%s AND migration_id=%s',(schema,f.name)).fetchone()[0]==0
    # Retry esgota quando lock persistir; checkpoint permanece em zero.
    f=Path(d)/'retry.ddl'; f.write_text('ALTER TABLE t ADD COLUMN retry_test integer;\n')
    with psycopg.connect(dbname=a.dbname,autocommit=True) as locker:
        locker.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        with locker.transaction():
            locker.execute('SELECT * FROM t')
            run('apply','--yes','--lock-policy','retry','--retry-interval','1','--retry-timeout','2',expected=2)
            assert n()==0
        logfile=Path(d)/'retry.log'
        argv=[sys.executable,str(Path(__file__).with_name('pgddl.py')),'apply',str(f),'-d',a.dbname,'--schema',schema,'--yes','--lock-policy','retry','--retry-interval','1','--retry-timeout','30','--log',str(logfile)]
        process=None
        try:
            with locker.transaction():
                locker.execute('SELECT * FROM t')
                process=subprocess.Popen(argv)
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    if process.poll() is not None: raise AssertionError('Runner encerrou antes de liberar lock')
                    if logfile.exists() and 'nova tentativa em' in logfile.read_text(): break
                    time.sleep(.1)
                else: raise AssertionError('Retry não observado')
                assert n()==0
                assert not conn.execute("SELECT EXISTS(SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid WHERE a.application_name='pgddl-runner' AND l.relation=to_regclass('t') AND NOT l.granted)").fetchone()[0]
            assert process.wait(timeout=35)==0
            assert n()==1
            assert conn.execute("SELECT (detail->>'lock_attempts')::int FROM _pgddl_runner.applied WHERE target_schema=%s AND migration_id=%s",(schema,f.name)).fetchone()[0]>=2
        finally:
            if process is not None and process.poll() is None:
                process.terminate(); process.wait(timeout=10)
    print('PASS: análise estrutural e retry sem fila, expiração e retomada.')
    print('PASS: dry run sem histórico, NOWAIT, conflito de leitura/escrita e retomada.')
    print('PASS: pausa, erro, retomada, sequence sem reset, hash, reexecução e reconhecimento manual.')
    print('O schema e histórico de teste foram preservados para auditoria:',schema)
