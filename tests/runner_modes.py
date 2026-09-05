"""Exercise the installed CLI, isolated custom agents, and file I/O end to end."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integration import request
from covenant import Client, Connection, ReferenceAgent
from covenant.transport import atomic_json


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--server',default='http://127.0.0.1:3001')
    args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='covenant-runner-modes-'))
    env={**os.environ, 'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    reports=[]
    for mode in ('reference','custom','files'):
        directory=root/mode
        directory.mkdir()
        seat=request(args.server,'/games',{'playerName':f'CLI {mode}','name':f'CLI {mode} test','turnSeconds':180})
        connection=Connection(args.server,seat['gameId'],seat['playerId'],seat['token'])
        connection.save(directory/'connection.json')
        client=Client(connection)
        client.command('fill-bots')
        client.command('start')
        command=[sys.executable,'-m','covenant','run','--connection',str(directory/'connection.json'),'--poll','0.25','--max-seconds','8','--state-dir',str(directory/'state')]
        if mode=='custom':
            (directory/'my_agent.py').write_text('from covenant import ReferenceAgent\nclass MyAgent(ReferenceAgent):\n    pass\n')
            command += ['--agent','my_agent:MyAgent']
        if mode=='files':
            command += ['--files',str(directory/'io')]
        with (directory/'runner.log').open('w') as log:
            process=subprocess.Popen(command,cwd=directory,env=env,stdout=log,stderr=log)
            while process.poll() is None:
                if mode=='files' and (directory/'io'/'observation.json').exists():
                    o=json.loads((directory/'io'/'observation.json').read_text())
                    if o['status']=='active':
                        atomic_json(directory/'io'/'orders.json',ReferenceAgent().decide(o))
                        atomic_json(directory/'io'/'outbox.json',{'turn':o['turn'],'commands':[{'id':'greeting','type':'message','data':{'to':'p2','text':'A file-based greeting.'}}]})
                time.sleep(.15)
            assert process.returncode==0,(mode,(directory/'runner.log').read_text())
        final=client.state()
        assert final['turn']>=3,(mode,final['turn'],(directory/'runner.log').read_text())
        assert any(s['owner']=='p1' and s['kind']=='resource' for s in final['structures']), 'Runner never captured a site'
        reports.append({'mode':mode,'gameId':seat['gameId'],'turn':final['turn'],'passed':True})
        print(json.dumps(reports[-1]),flush=True)
    Path('/tmp/covenant-runner-modes.json').write_text(json.dumps(reports,indent=2))


if __name__=='__main__':
    main()
