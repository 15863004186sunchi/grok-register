import os
import time
import requests

YESCAPTCHA_SOFT_ID = 102154


class TurnstileService:
    """
    通过 YesCaptcha API 自动解决 Cloudflare Turnstile 验证。
    API 文档: https://yescaptcha.com/
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv('YESCAPTCHA_KEY', '').strip()
        self.api_base = "https://api.yescaptcha.com"
        if not self.api_key:
            raise Exception("缺少 YESCAPTCHA_KEY（可通过环境变量或 config.json 配置）")

    def create_task(self, site_url: str, site_key: str, action: str = None, data: str = None) -> str:
        """提交验证任务，返回 task_id"""
        payload = {
            "clientKey": self.api_key,
            "task": {
                "type": "TurnstileTaskProxyless",
                "websiteURL": site_url,
                "websiteKey": site_key
            },
            "softID": YESCAPTCHA_SOFT_ID,
        }
        if action:
            payload["task"]["pageAction"] = action
        if data:
            payload["task"]["pageData"] = data
            
        resp = requests.post(f"{self.api_base}/createTask", json=payload, timeout=15)
        resp.raise_for_status()
        data_resp = resp.json()
        if data_resp.get('errorId') != 0:
            raise Exception(f"YesCaptcha 创建任务失败: {data_resp.get('errorDescription')}")
        task_id = data_resp['taskId']
        print(f"[YesCaptcha] 任务已提交: {task_id} (action={action})")
        return task_id

    def get_response(self, task_id: str, max_retries: int = 30,
                     initial_delay: float = 5, retry_delay: float = 2) -> str | None:
        """轮询直到 token 就绪"""
        time.sleep(initial_delay)
        for attempt in range(max_retries):
            try:
                payload = {"clientKey": self.api_key, "taskId": task_id}
                resp = requests.post(f"{self.api_base}/getTaskResult", json=payload, timeout=15)
                resp.raise_for_status()
                data = resp.json()

                if data.get('errorId') != 0:
                    print(f"[YesCaptcha] 获取结果失败: {data.get('errorDescription')}")
                    return None

                status = data.get('status')
                if status == 'ready':
                    token = data.get('solution', {}).get('token')
                    if token:
                        print(f"[YesCaptcha] ✓ Token 获取成功 (第 {attempt+1} 次): {token[:30]}...")
                        return token
                    print("[YesCaptcha] 结果中无 token")
                    return None
                elif status == 'processing':
                    print(f"[YesCaptcha] 处理中... ({attempt+1}/{max_retries})")
                    time.sleep(retry_delay)
                else:
                    print(f"[YesCaptcha] 未知状态: {status}")
                    time.sleep(retry_delay)
            except Exception as e:
                print(f"[YesCaptcha] 轮询异常 ({attempt+1}/{max_retries}): {e}")
                time.sleep(retry_delay)

        print("[YesCaptcha] 超时，未获取到 token")
        return None

    def solve(self, site_url: str, site_key: str, action: str = None, data: str = None) -> str | None:
        """一步完成：提交任务 + 等待结果"""
        task_id = self.create_task(site_url, site_key, action=action, data=data)
        return self.get_response(task_id)
