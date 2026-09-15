#!/usr/bin/env python3
import argparse
import os
from app.server import create_app

def main():
    parser=argparse.ArgumentParser(description='AI 外贸工作台（仅本机访问）')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    app=create_app()
    print(f'AI 外贸工作台: http://127.0.0.1:{args.port}',flush=True)
    app.run(host='127.0.0.1',port=args.port,debug=False,use_reloader=False,threaded=True,load_dotenv=False)
if __name__=='__main__':main()
