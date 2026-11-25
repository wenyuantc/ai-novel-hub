"""在隔离机器的本地页面核对管理员后台显示的存活进程。"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    stage = "credentials"
    try:
        from playwright.sync_api import sync_playwright

        values = {}
        for line in args.env_file.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        stage = "launch"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(service_workers="block")

            def allow_local(route):
                url = urlsplit(route.request.url)
                if url.scheme == "http" and url.hostname == "127.0.0.1" and url.port == args.port:
                    route.continue_()
                else:
                    route.abort()

            context.route("**/*", allow_local)
            page = context.new_page()
            base = f"http://127.0.0.1:{args.port}"
            stage = "login"
            page.goto(f"{base}/login", wait_until="domcontentloaded")
            page.get_by_placeholder("请输入用户名").fill(values["ADMIN_USERNAME"])
            page.get_by_placeholder("请输入密码").fill(values["ADMIN_PASSWORD"])
            page.get_by_role("button", name="登录知页").click()
            page.wait_for_url(re.compile(r"/projects(?:\?.*)?$"), timeout=30000)
            stage = "settings"
            page.goto(f"{base}/admin/system-settings", wait_until="domcontentloaded")
            page.get_by_role("heading", name="任务排空状态").wait_for(timeout=30000)
            stage = "workers"
            page.wait_for_function("document.querySelectorAll('.task-drain-workers li').length === 2", timeout=30000)
            workers = []
            for item in page.locator(".task-drain-workers li strong").all_text_contents():
                match = re.fullmatch(r"(.+)（PID ([0-9]+)）", item)
                if not match:
                    raise ValueError("invalid worker display")
                workers.append({"hostname": match.group(1), "pid": int(match.group(2))})
            context.close()
            browser.close()
        if len({(worker["hostname"], worker["pid"]) for worker in workers}) != 2:
            raise ValueError("invalid worker count")
        print(json.dumps({"ui_page": True, "workers": workers}))
        return 0
    except Exception:
        # 页面、请求、输入和异常原文都不得写入报告。
        print(json.dumps({"ui_page": False, "error": "ui_probe_failed", "stage": stage}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
