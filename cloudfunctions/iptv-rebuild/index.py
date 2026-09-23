#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""已退役的 CloudBase IPTV 定时写入任务。"""


def main_handler(event, context):
    return {"ret": 0, "retired": True, "msg": "IPTV rebuild is owned by the self-hosted service"}


main = main_handler
