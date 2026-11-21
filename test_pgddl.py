import tempfile
import unittest
from pathlib import Path
from pgddl import load_manifest, RunnerError, target, canonical_name, CONFLICTS, recursive_target, dry_run, simple_type, column_request, index_request, structural_findings, retry_prelock
from unittest.mock import patch
from types import SimpleNamespace

class Tests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'test.ddl'; p.write_bytes(data)
            return load_manifest(p)
    def test_multiline_encoding(self):
        m=self.load(b'-- In\xedcio;\r\nCREATE TABLE a (\r\n x integer\r\n);\r\nALTER TABLE a ADD COLUMN y varchar(10);')
        self.assertEqual(m.encoding,'cp1252')
        self.assertEqual([x.line for x in m.steps],[2,5])
        self.assertEqual([x.kind for x in m.steps],['CREATE TABLE','ALTER TABLE ADD COLUMN'])
    def test_strings_comments(self):
        m=self.load(b"/* outer /* ; */ */ CREATE TABLE a (x text DEFAULT 'a;''b'); -- ;\nCREATE INDEX ix ON a (x);")
        self.assertEqual(len(m.steps),2)
        self.assertIn("'a;''b'",m.steps[0].sql)
    def test_bad(self):
        for s in [b'DROP TABLE t;',b'CREATE INDEX CONCURRENTLY i ON t(x);',b'ALTER TABLE t ADD x int, DROP y;',b'CREATE TABLE t(x int)', b'CREATE TABLE t(x text DEFAULT $$abc$$);',b'CREATE TABLE IF NOT EXISTS t(x int);',b'CREATE TABLE t AS SELECT 1;',b'CREATE TABLE t(x text DEFAULT E\'a\');',b'CREATE TABLE t(x int);\\q',b'CREATE SEQUENCE _pgddl_runner.x;',b'CREATE TABLE t(x text DEFAULT \'a);']:
            with self.subTest(s=s), self.assertRaises(RunnerError): self.load(s)
    def test_sequence(self):
        m=self.load(b'CREATE SEQUENCE s; ALTER SEQUENCE s RESTART WITH 1;')
        self.assertEqual(m.steps[1].kind,'ALTER SEQUENCE RESTART')
    def test_hash_file_and_steps(self):
        a=self.load(b'CREATE TABLE t(x int);'); b=self.load(b'-- comment\nCREATE TABLE t(x int);')
        self.assertNotEqual(a.digest,b.digest)
        self.assertEqual(a.steps[0].digest,b.steps[0].digest)
    def test_unique_index(self):
        self.assertEqual(self.load(b'CREATE UNIQUE INDEX i ON t(x);').steps[0].kind,'CREATE UNIQUE INDEX')
    def test_quoted_semicolon(self):
        m=self.load(b'CREATE TABLE a ("x;y" int);')
        self.assertEqual(len(m.steps),1)
    def test_no_commands(self):
        with self.assertRaises(RunnerError): self.load(b'-- Inicio;\n-- fim;')

    def test_target_and_modes(self):
        m=self.load(b'ALTER TABLE ONLY "Public"."Users" ADD COLUMN x int; CREATE UNIQUE INDEX ix ON t(x); ALTER SEQUENCE s RESTART WITH 1;')
        a,b,c=map(target,m.steps)
        self.assertEqual(canonical_name(a.name,'public'),('Public','Users'))
        self.assertTrue(a.only); self.assertEqual(a.mode,'ACCESS EXCLUSIVE')
        self.assertEqual(b.name,'t'); self.assertEqual(b.mode,'SHARE')
        self.assertEqual(c.mode,'SHARE ROW EXCLUSIVE')
    def test_conflict_matrix(self):
        self.assertIn('AccessShareLock',CONFLICTS['ACCESS EXCLUSIVE'])
        self.assertNotIn('AccessShareLock',CONFLICTS['SHARE'])
        self.assertNotIn('RowShareLock',CONFLICTS['SHARE'])
        self.assertIn('RowExclusiveLock',CONFLICTS['SHARE'])
        self.assertNotIn('ShareLock',CONFLICTS['SHARE'])
        self.assertIn('ShareLock',CONFLICTS['SHARE ROW EXCLUSIVE'])
    def test_recursive_scope(self):
        a,b,c=map(target,self.load(b'ALTER TABLE t ADD x int; CREATE INDEX ix ON t(x); CREATE INDEX iy ON ONLY t(x);').steps)
        self.assertTrue(recursive_target(a,(1,'r')))
        self.assertFalse(recursive_target(b,(1,'r')))
        self.assertTrue(recursive_target(b,(1,'p')))
        self.assertFalse(recursive_target(c,(1,'p')))
    def test_dry_run_dependencies(self):
        m=self.load(b'CREATE TABLE t(x int); CREATE INDEX ix ON t(x); CREATE SEQUENCE s; ALTER SEQUENCE s RESTART WITH 1;')
        args=SimpleNamespace(schema='public',max_steps=0,verbose=False,locks_only=True)
        with patch('pgddl.read_state',return_value=None),patch('pgddl.relation_info',return_value=None),patch('pgddl.lock_snapshot',return_value=[]):
            self.assertEqual(dry_run(None,args,m,('public','lab'),'postgres'),0)
    def test_dry_run_conflict(self):
        m=self.load(b'ALTER TABLE t ADD x int;')
        args=SimpleNamespace(schema='public',max_steps=0,verbose=False,locks_only=True)
        rows=[('t',123,'AccessShareLock',True,'u','erp','1.2.3.4','idle in transaction',20,None,None)]
        with patch('pgddl.read_state',return_value=None),patch('pgddl.relation_info',return_value=(1,'r')),patch('pgddl.lock_snapshot',return_value=rows), self.assertLogs('pgddl',level='INFO') as log:
            self.assertEqual(dry_run(None,args,m,('public','lab'),'postgres'),4)
            self.assertIn('PID=123',' '.join(log.output))

    def test_simple_types(self):
        self.assertEqual(simple_type('VARCHAR ( 11 )'),'character varying(11)')
        self.assertEqual(simple_type('NUMERIC(12)'),'numeric(12,0)')
        self.assertIsNone(simple_type('integer NOT NULL DEFAULT 0'))
        m=self.load(b'ALTER TABLE usuarios ADD COLUMN Usua_cpf VARCHAR(11); ALTER TABLE t ALTER COLUMN x TYPE TEXT;')
        self.assertEqual(column_request(m.steps[0]),('usua_cpf','character varying(11)'))
        self.assertEqual(column_request(m.steps[1]),('x','text'))
    def test_index_request(self):
        m=self.load(b'CREATE INDEX ix ON t(a,b); CREATE UNIQUE INDEX iu ON t(a); CREATE INDEX ic ON t(lower(a));')
        self.assertEqual(index_request(m.steps[0]),('ix',('a','b'),False))
        self.assertEqual(index_request(m.steps[1]),('iu',('a',),True))
        self.assertIsNone(index_request(m.steps[2]))
    def test_retry_rules(self):
        e=SimpleNamespace(sqlstate='55P03')
        self.assertEqual(retry_prelock(e,'prelock','retry',100,5,90),5)
        self.assertEqual(retry_prelock(e,'prelock','retry',100,5,98),2)
        for phase in ('begin','ddl','checkpoint','commit'):
            self.assertIsNone(retry_prelock(e,phase,'retry',100,5,90))
        for policy in ('nowait','wait'):
            self.assertIsNone(retry_prelock(e,'prelock',policy,100,5,90))
        self.assertIsNone(retry_prelock(e,'prelock','retry',100,5,100))
        self.assertIsNone(retry_prelock(SimpleNamespace(sqlstate='40P01'),'prelock','retry',100,5,90))
    def test_structural_existing_column(self):
        s=self.load(b'ALTER TABLE t ADD x varchar(11);').steps[0]
        cache={1:((10,100,8192),{'x':('character varying(10)',False,False)},[])}
        rows=structural_findings(s,None,(1,'r'),cache,{}, {}, ('public','t'),set())
        self.assertEqual(rows[0][0],'COLUNA_EXISTENTE')
        self.assertIn('divergente',rows[0][1]); self.assertTrue(rows[0][2])
    def test_planned_columns(self):
        m=self.load(b'ALTER TABLE t ADD x integer; ALTER TABLE t ADD x integer;')
        cache={1:((10,100,8192),{},[])}; planned={}
        first,second=[structural_findings(s,None,(1,'r'),cache,planned,{},('public','t'),set()) for s in m.steps]
        self.assertEqual(first,[]); self.assertEqual(second[0][0],'COLUNA_REPETIDA_NO_ARQUIVO')
    def test_index_candidates(self):
        from unittest.mock import Mock
        s=self.load(b'CREATE INDEX ix ON t(a,b);').steps[0]
        conn=Mock(); conn.execute.return_value.fetchone.return_value=None
        cache={1:((100,1000,8192),{'a':('integer',False,False),'b':('integer',False,False)},[
            ('old',False,True,'btree',True,['a','b']),('reverse',False,True,'btree',True,['b','a']),
            ('invalid',False,False,'btree',True,['a','b']),('unique',True,True,'btree',True,['a','b'])])}
        rows=structural_findings(s,conn,(1,'r'),cache,{}, {}, ('public','t'),set())
        candidates=[r for r in rows if r[0]=='INDICE_CANDIDATO_REDUNDANTE']
        self.assertEqual(len(candidates),1); self.assertIn('old',candidates[0][1])
        self.assertIn('IMPACTO_ESTIMADO',[r[0] for r in rows])
    def test_dry_run_structural_alert(self):
        m=self.load(b'ALTER TABLE t ADD x integer;')
        args=SimpleNamespace(schema='public',max_steps=0,verbose=False,locks_only=False)
        with patch('pgddl.read_state',return_value=None),patch('pgddl.relation_info',return_value=(1,'r')),patch('pgddl.lock_snapshot',return_value=[]),patch('pgddl.structural_snapshot',return_value=((0,-1,8192),{'x':('integer',False,False)},[])),self.assertLogs('pgddl',level='INFO') as log:
            self.assertEqual(dry_run(None,args,m,('public','lab'),'postgres'),4)
            self.assertIn('COLUNA_EXISTENTE',' '.join(log.output))

if __name__=='__main__': unittest.main()
