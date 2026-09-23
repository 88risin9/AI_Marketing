#!/usr/bin/env python3
import argparse
import threading
from app.server import create_app
from app.portal import create_buyer_app
from werkzeug.serving import make_server, WSGIRequestHandler


class QuietRequestHandler(WSGIRequestHandler):
    # Do not log buyer URLs, contact information, or submitted purchase lists.
    def log_request(self, code='-', size='-'):
        pass

def main():
    parser=argparse.ArgumentParser(description='AI 外贸工作台 V2（内部工作台与买家预览仅本机访问）')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--buyer-port',type=int,default=8766)
    parser.add_argument('--data-dir',help='独立数据目录；默认沿用 data/ 中的 V1 数据')
    args=parser.parse_args()
    if not all(1024 <= port <= 65535 for port in (args.port,args.buyer_port)) or args.port==args.buyer_port:
        parser.error('两个端口需不同，且均为 1024–65535。')
    app=create_app(args.data_dir)
    buyer=create_buyer_app(args.data_dir)
    app.config['BUYER_PORT']=args.buyer_port
    buyer.config['BUYER_PORT']=args.buyer_port
    internal_server=make_server('127.0.0.1',args.port,app,threaded=True,request_handler=QuietRequestHandler)
    try:
        buyer_server=make_server('127.0.0.1',args.buyer_port,buyer,threaded=True,request_handler=QuietRequestHandler)
    except BaseException:
        internal_server.server_close()
        raise
    thread=threading.Thread(target=buyer_server.serve_forever,daemon=True,name='buyer-preview')
    thread.start()
    print(f'AI 外贸工作台 V2: http://127.0.0.1:{args.port}',flush=True)
    print(f'买家页本地预览: http://127.0.0.1:{args.buyer_port}（尚未公开上线）',flush=True)
    print('按 Ctrl+C 停止。资料与任务进度保存在 SQLite 中。',flush=True)
    try:
        internal_server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.extensions['research_manager'].stop()
        buyer_server.shutdown()
        buyer_server.server_close()
        internal_server.server_close()
        thread.join(timeout=2)
if __name__=='__main__':main()
