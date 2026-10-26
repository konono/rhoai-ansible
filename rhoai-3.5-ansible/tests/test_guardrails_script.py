"""Exercise the shell probe with a mocked oc, including false-success regressions."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'tools/test-guardrails.sh'
MOCK = r'''
import json,os,subprocess,sys
args=sys.argv[1:]
if args[:2]==['get','nemoguardrails']:
 print('Ready',end='')
elif args[:2]==['get','pods']:
 print('nemo-guardrails-test 1/1 Running 0 1h')
elif args[:2]==['get','configmap']:
 print('models:\n- type: main\n  model: configured-model')
elif args[:2]==['get','llminferenceservice']:
 print(json.dumps({'items':[{'metadata':{'name':'unrelated-model'},'status':{'conditions':[{'type':'Ready','status':'True'}]}}]}))
elif 'python3' in args:
 sys.exit(subprocess.run([sys.executable,*args[args.index('python3')+1:]],stdin=sys.stdin).returncode)
elif 'curl' in args:
 if any('/v1/health' in x for x in args):
  print('{"status":"pass"}')
 else:
  data=json.loads(args[args.index('-d')+1]); mode=os.environ.get('PROBE_MODE','ok')
  if data['model']!='configured-model':
   content='Internal server error'
  elif 'bomb' in data['messages'][0]['content']:
   content='I am sorry, I cannot help with that request.'
  else:
   content='Internal server error' if mode=='server-error-content' else 'Tokyo'
   if mode=='empty-timeout':
    sys.exit(28)
   if mode=='transport-error':
    print(json.dumps({'choices':[{'message':{'content':content}}]}));sys.exit(28)
   if mode=='http-error' and '-skf' in args:
    sys.exit(22)
  print(json.dumps({'choices':[{'message':{'content':content}}]}))
else:
 sys.exit('unexpected oc invocation: '+repr(args))
'''


class GuardrailsProbe(unittest.TestCase):
    def run_probe(self, mode):
        with tempfile.TemporaryDirectory() as td:
            oc=Path(td)/'oc';oc.write_text('#!'+sys.executable+'\n'+MOCK);oc.chmod(0o755)
            env=dict(os.environ, PATH=td+os.pathsep+os.environ['PATH'], PROBE_MODE=mode)
            return subprocess.run(['bash',str(SCRIPT)],text=True,capture_output=True,env=env,timeout=20)

    def test_uses_configured_model_in_multi_model_cluster(self):
        p=self.run_probe('ok')
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertIn('configured-model',p.stdout)
        self.assertIn('OK 3 / NG 0',p.stdout)

    def test_server_error_content_is_not_inference_success(self):
        p=self.run_probe('server-error-content')
        self.assertNotEqual(p.returncode,0,p.stdout)
        self.assertIn('期待した回答ではありません',p.stdout)

    def test_partial_body_after_timeout_is_not_success(self):
        p=self.run_probe('transport-error')
        self.assertNotEqual(p.returncode,0,p.stdout)
        self.assertIn('Colang', p.stdout)
        self.assertIn('TIMEOUT=600', p.stdout)
        self.assertNotIn('通常推論の HTTP/通信エラー', p.stdout)

    def test_empty_timeout_retains_diagnostics(self):
        p=self.run_probe('empty-timeout')
        self.assertNotEqual(p.returncode,0,p.stdout)
        self.assertIn('通常推論が 180s でタイムアウト', p.stdout)
        self.assertIn('oc scale deploy', p.stdout)

    def test_http_error_is_not_success(self):
        p=self.run_probe('http-error')
        self.assertNotEqual(p.returncode,0,p.stdout)
        self.assertIn('通常推論の HTTP/通信エラー (curl rc=22)', p.stdout)


if __name__=='__main__':
    unittest.main()
