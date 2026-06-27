#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
站点爬虫抽象基类。

新站点继承 BaseSpider，只需实现 4 个抽象方法即可：
  - list_url(page)                : 构造列表页 URL
  - detail_url(native_id)         : 构造详情页 URL
  - parse_list(html)              : 从列表页 HTML 提取详情页 native_id 列表
  - parse_detail(html, native_id) : 从详情页 HTML 提取字段 dict（失败返 None）

HTTP 请求 / 代理 / 反爬退避 / 并发 / CSV / 数据库导入等基础设施
当前不在基类里——只有 1 个站点时凭空抽中间层是过度工程。
等接入第二个站点、出现真实复用需求时，再把共性下沉到 core/。
"""

from abc import ABC, abstractmethod
from typing import Optional


class BaseSpider(ABC):
    """所有站点爬虫的契约。"""

    # ---- 子类必须设置 ----
    site_name: str = ""   # 用作日志/CSV列/状态标识，例如 "28car"
    base_url: str = ""    # 站点根 URL，例如 "https://dj1jklak2e.28car.com"

    # ---- 子类必须实现 ----
    @abstractmethod
    def list_url(self, page: int) -> str:
        """构造列表页 URL。"""

    @abstractmethod
    def detail_url(self, native_id: str) -> str:
        """根据 native_id 构造详情页 URL。"""

    @abstractmethod
    def parse_list(self, html: str) -> list:
        """从列表页 HTML 提取所有详情页 native_id，返回列表。"""

    @abstractmethod
    def parse_detail(self, html: str, native_id: str) -> Optional[dict]:
        """
        从详情页 HTML 提取字段 dict。失败返回 None。

        建议返回的字段名与 CSV/DB 列保持一致（vehicle_id / car_brand / price 等），
        由子类自行做字段映射。
        """

    # ---- 基类提供，子类一般不重写 ----
    def vehicle_id(self, native_id: str) -> str:
        """
        构造全局唯一的 vehicle_id（数据库主键）。

        【28car 历史包袱 — 不要"修复"】
        28car 的现存 11 万+ 历史数据 vehicle_id 未带前缀（如 "s2710191"），
        且 vehicle_images.vehicle_id 有外键 ON UPDATE RESTRICT 约束，无法
        批量改写。所以 28car 保持 native_id 原值；新站点统一用站点前缀。

        【新站点如何避免 ID 冲突】
        BaseSpider 默认实现返回 "{site_name}_{native_id}"
        （例如 autohome 站点：vehicle_id="autohome_a123"）。
        """
        if self.site_name == "28car":
            return native_id
        return f"{self.site_name}_{native_id}"
