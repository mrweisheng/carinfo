#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
补爬脚本 - 只爬取缺失的页面
用于补充之前因网络问题跳过的数据

当前缺失:
- 私家车: 24-100页
- 客货车(类型2): 1-30页
- 货车(类型3): 1-30页
- 电单车(类型4): 1-30页
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from carinfo import scrape_vehicle_type
import time


def main():
    print("=" * 60)
    print("         补爬脚本 - 补充缺失数据")
    print("=" * 60)
    print()

    # 补爬任务配置
    tasks = [
        {'type': 1, 'name': '私家车', 'start_page': 24, 'pages': 100},
        {'type': 2, 'name': '客货车', 'start_page': 1, 'pages': 30},
        {'type': 3, 'name': '货车', 'start_page': 1, 'pages': 30},
        {'type': 4, 'name': '电单车', 'start_page': 1, 'pages': 30},
    ]

    total_vehicles = 0
    total_start_time = time.time()

    for task in tasks:
        vehicle_type = task['type']
        type_name = task['name']
        start_page = task['start_page']
        total_pages = task['pages']
        csv_filename = f"car_data_{vehicle_type}.csv"

        print("=" * 60)
        print(f"开始补爬: {type_name} (类型{vehicle_type}) - 第{start_page}-{total_pages}页")
        print("=" * 60)

        start_time = time.time()

        try:
            count = scrape_vehicle_type(vehicle_type, total_pages, csv_filename, start_page=start_page)
            total_vehicles += count
            elapsed = time.time() - start_time

            print()
            print(f"[OK] {type_name} 补爬完成: {count} 条记录, 耗时 {elapsed/60:.1f} 分钟")
            print()

        except Exception as e:
            print(f"[ERROR] {type_name} 补爬失败: {e}")
            import traceback
            traceback.print_exc()
            print()

        # 休息一下再爬下一个类型
        if task != tasks[-1]:
            rest_time = 10
            print(f"休息 {rest_time} 秒，准备爬取下一个类型...")
            time.sleep(rest_time)

    total_elapsed = time.time() - total_start_time

    print("=" * 60)
    print("              补爬完成！")
    print("=" * 60)
    print(f"总计获取车辆: {total_vehicles} 条")
    print(f"总耗时: {total_elapsed/60:.1f} 分钟")
    print()
    print("准备导入数据库...")

    # 自动导入数据库
    from carinfo import auto_import_to_database
    auto_import_to_database()

    print()
    print("=" * 60)
    print("              所有任务完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
