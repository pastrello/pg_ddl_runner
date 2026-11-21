"""Regressões da CLI: ajuda offline e parâmetros de execução preservados."""
import contextlib
import io
import unittest
import pgddl

class HelpTests(unittest.TestCase):
    def help_text(self, argv):
        out=io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
            pgddl.main([*argv, '-h'])
        self.assertEqual(e.exception.code,0)
        return out.getvalue()

    def test_general_help(self):
        text=self.help_text([])
        for token in ('Uso: pgddl','Comandos disponíveis','plan','dry-run','status','apply','ack','dependencies','report','validate','Códigos de saída','130'):
            self.assertIn(token,text)

    def test_all_subcommand_help(self):
        for cmd in ('plan','dry-run','status','apply','ack','dependencies','report','validate'):
            with self.subTest(command=cmd):
                text=self.help_text([cmd])
                self.assertIn('ARQUIVO.ddl',text)
                self.assertIn('--encoding',text)
                self.assertIn('Exemplo',text)
                if cmd!='plan':
                    self.assertIn('--dbname',text)
                    self.assertIn('--schema',text)

    def test_apply_defaults(self):
        a=pgddl.build_parser().parse_args(['apply','x.ddl','-d','erp'])
        self.assertEqual((a.schema,a.encoding,a.lock_timeout,a.statement_timeout,a.dependency_policy,a.lock_policy,a.retry_interval,a.retry_timeout,a.monitor_interval,a.max_steps),('public','auto',3,0,'stop','nowait',5,300,2,0))
        self.assertFalse(a.yes or a.continue_on_warning or a.verbose)
        self.assertIsNone(a.parallel_maintenance_workers)
        self.assertIsNone(a.maintenance_work_mem)

    def test_aliases(self):
        a=pgddl.build_parser().parse_args(['apply','x.ddl','--dbname','erp','--host','localhost','--port','15777','--user','postgres','--max-parallel-maintenance-workers','4'])
        self.assertEqual((a.dbname,a.host,a.port,a.user,a.parallel_maintenance_workers),('erp','localhost',15777,'postgres',4))

if __name__=='__main__': unittest.main()
