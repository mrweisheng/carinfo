import argparse

from .service import CarinfoService


def main():
    parser = argparse.ArgumentParser(description="carinfo 调度服务（前台运行，实时日志）")
    parser.add_argument("--lock-file", default="carinfo_run.lock", help="运行互斥锁文件路径")
    parser.add_argument("--state-file", default=".carinfo_service_state.json", help="调度状态文件路径")
    args = parser.parse_args()

    service = CarinfoService(lock_file=args.lock_file, state_file=args.state_file)
    service.run_forever()
 
