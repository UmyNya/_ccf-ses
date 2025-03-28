# -*- coding: UTF-8 -*-

import json
import logging
import threading
import time
from xml.etree import ElementTree as ET
import requests
import urllib3

from storage_evaluation_system_zzj.client.client import Client, ClientBuilder
from storage_evaluation_system_zzj.util import JsonObject
from storage_evaluation_system_zzj import parameter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


class HttpClient(Client):
    port = 80
    connector: requests.Session

    def __init__(self, env_config: ET.Element):
        super().__init__(env_config)
        self.login_func = None
        self.connector.trust_env = False
        if self.parameters.get("http_reconnect_timeout"):
            self.relogin_duration = int(self.parameters.get("http_reconnect_timeout"))
        else:
            self.relogin_duration = parameter.DefaultParameter.HTTP_RECONNECT_TIMEOUT.value
        self._last_quest_time = time.time()
        self._lock = threading.Lock()
        self._in_relogin = False

    def connect(self):
        return requests.Session()

    def update_headers(self, **kwargs):
        self.connector.headers.update(**kwargs)

    def _build_url(self, uri: str, query_params: dict = None):
        """处理url"""
        # 自处理路径参数，便于在请求前打印完整url
        if query_params:
            query_str = "&".join([f"{k}={v}" for k, v in query_params.items()])
            uri += "/" + query_str

        return f"{self.protocol}://{self.ip}:{self.port}/{uri}"

    def _try_relogin(self):
        """http自动超时重连"""
        now = time.time()
        duration = now - self._last_quest_time
        self._last_quest_time = time.time()
        if duration > self.relogin_duration:
            self.logger.debug(f"{self} may be expired, reconnecting")
            self._in_relogin = True
            self.login_func()
            self._in_relogin = False

    def request(self, method, uri, query_params: dict = None, data=None, headers=None, timeout=120, **kwargs):
        """http请求 """
        if self.login_func and not self._in_relogin:
            with self._lock:
                self._try_relogin()

        url = self._build_url(uri, query_params)
        self.logger.debug(f">> [{method}] {url}")
        response = self.connector.request(method=method,
                                          url=url,
                                          data=data,
                                          headers=headers,
                                          timeout=timeout,
                                          verify=False,
                                          **kwargs)

        try:
            out_resp = decoded_str = response.content.decode("utf-8")
            if "application/json" in response.headers.get("content-type").lower():
                out_resp = json.loads(decoded_str)
                try:
                    response.json_data = response.json()  # 保存原数据
                    response.json = JsonObject(response.json())
                except:
                    pass
        except Exception:
            out_resp = response.content

        self.logger.debug(f"<< {response.status_code} {out_resp}")

        return response

    def get(self, uri, **kwargs):
        return self.request('GET', uri, **kwargs)

    def post(self, uri, **kwargs):
        return self.request('POST', uri, **kwargs)

    def put(self, uri, **kwargs):
        return self.request('PUT', uri, **kwargs)

    def delete(self, uri, **kwargs):
        return self.request('DELETE', uri, **kwargs)

    def patch(self, uri, **kwargs):
        return self.request('PATCH', uri, **kwargs)

    def head(self, uri, **kwargs):
        return self.request('HEAD', uri, **kwargs)

    def close(self):
        pass


class HttpClientBuilder(ClientBuilder):

    def create_client(self) -> HttpClient:
        return HttpClient(self.env_config)
