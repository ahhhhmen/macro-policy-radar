"""
统一 DeepSeek API 缓存客户端 — v1.0

三层缓存架构：
  1. 进程内存去重（dict[hash] → response）—— 杜绝同次运行重复调用
  2. 磁盘缓存（diskcache）—— 跨运行复用，按 task_type 设置不同 TTL
  3. DeepSeek API 兜底

用法:
    from llm_cache import CachedLLMClient

    client = CachedLLMClient()
    response = client.chat_completion(
        task_type="policy_extraction",
        model="deepseek-v4-pro",
        messages=[{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    print(client.get_stats())
"""

import os
import json
import hashlib
import threading
from datetime import datetime, timezone
from openai import OpenAI


# TTL 策略（秒）：按任务类型区分
TTL_MAP = {
    "hotspot_discovery":   24 * 3600,   # 24h - 热点问题每天相同
    "policy_extraction":   72 * 3600,   # 72h - 同一文章三天内重扫
    "baseline_audit":     168 * 3600,   # 7d  - 审计周期长
    "default":             24 * 3600,   # 默认 24h
}

# 缓存目录
CACHE_DIR = os.path.join(
    os.path.expanduser("~"), ".cache", "macro-policy-radar", "llm_cache"
)


def _make_cache_key(model, messages, temperature, extra_params=None):
    """
    生成确定性缓存 key（SHA256）。
    
    将请求参数规范化为 JSON → SHA256，确保：
    - 相同 prompt + 相同参数 → 相同 key
    - 不同 temperature → 不同 key（服务端缓存隔离）
    - 不同 response_format → 不同 key
    """
    # 深度序列化 messages：确保 dict 键排序一致
    canonical = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if extra_params:
        canonical["extra"] = extra_params
    raw = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CachedLLMClient:
    """
    统一 DeepSeek API 客户端，内置内存去重 + 磁盘缓存。
    
    - 线程安全（锁保护内存缓存字典）
    - 磁盘缓存自动过期（diskcache 原生支持）
    - 提供 stats 方法输出命中率
    """

    def __init__(self, cache_dir=None):
        self._cache_dir = cache_dir or CACHE_DIR
        os.makedirs(self._cache_dir, exist_ok=True)

        # ---- 第 1 层：进程内存缓存 ----
        self._memory_cache = {}          # key_hash → response_json_str
        self._lock = threading.Lock()

        # ---- 第 2 层：磁盘缓存 ----
        # 延迟导入，避免 diskcache 未安装时直接崩溃
        import diskcache
        self._disk = diskcache.Cache(self._cache_dir)

        # ---- 第 3 层：DeepSeek API 客户端（复用 OpenAI Client） ----
        self._client = OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
            max_retries=2,
        )

        # ---- 统计 ----
        self._stats = {"memory_hits": 0, "disk_hits": 0, "api_calls": 0}

    # ------------------------------------------------------------------
    #  公共接口
    # ------------------------------------------------------------------

    def chat_completion(
        self,
        task_type,
        model="deepseek-v4-pro",
        messages=None,
        temperature=0.0,
        **kwargs,
    ):
        """
        执行一次带缓存的 Chat Completion 调用。

        参数:
            task_type: "hotspot_discovery" | "policy_extraction" | "baseline_audit"
            model:     模型名称
            messages:  OpenAI 格式的 messages 列表
            temperature: 温度参数（推荐 0 以提升缓存命中率）
            **kwargs:  其他 OpenAI 参数（response_format, timeout 等）

        返回:
            OpenAI ChatCompletion 对象（与 openai SDK 返回类型一致）
        """
        if messages is None:
            raise ValueError("messages 不能为空")

        # 提取影响缓存 key 的额外参数
        extra_for_key = {}
        for k in ("response_format", "max_tokens", "top_p", "frequency_penalty", "presence_penalty"):
            if k in kwargs:
                extra_for_key[k] = kwargs[k]

        cache_key = _make_cache_key(model, messages, temperature, extra_for_key)

        # ---- 第 1 层：内存缓存 ----
        with self._lock:
            if cache_key in self._memory_cache:
                self._stats["memory_hits"] += 1
                return self._deserialize_response(self._memory_cache[cache_key])

        # ---- 第 2 层：磁盘缓存 ----
        ttl = TTL_MAP.get(task_type, TTL_MAP["default"])
        disk_val = self._disk.get(cache_key, default=None)
        if disk_val is not None:
            # 磁盘命中 → 回填内存缓存
            with self._lock:
                self._memory_cache[cache_key] = disk_val
            self._stats["disk_hits"] += 1
            return self._deserialize_response(disk_val)

        # ---- 第 3 层：DeepSeek API ----
        self._stats["api_calls"] += 1
        response = self._client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            **kwargs,
        )

        # 写入缓存（内存 + 磁盘）
        serialized = self._serialize_response(response)
        with self._lock:
            self._memory_cache[cache_key] = serialized
        self._disk.set(cache_key, serialized, expire=ttl)

        return response

    def get_stats(self):
        """返回当前缓存命中统计"""
        total = self._stats["memory_hits"] + self._stats["disk_hits"] + self._stats["api_calls"]
        hit_rate = (
            (self._stats["memory_hits"] + self._stats["disk_hits"]) / total * 100
            if total > 0
            else 0.0
        )
        return {
            **self._stats,
            "total_requests": total,
            "hit_rate_pct": round(hit_rate, 1),
        }

    def print_stats(self):
        """打印人类可读的缓存统计"""
        s = self.get_stats()
        print(
            f"\n📊 [LLM Cache] 统计: "
            f"总计 {s['total_requests']} 次请求 | "
            f"内存命中 {s['memory_hits']} | "
            f"磁盘命中 {s['disk_hits']} | "
            f"API 调用 {s['api_calls']} | "
            f"命中率 {s['hit_rate_pct']}%"
        )

    # ------------------------------------------------------------------
    #  序列化 / 反序列化（OpenAI ChatCompletion → JSON → 重建对象）
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_response(response):
        """将 OpenAI ChatCompletion 对象序列化为 JSON 字符串"""
        try:
            # response.model_dump_json() 是 openai SDK v1.x 的标准序列化方法
            return response.model_dump_json()
        except AttributeError:
            # 兜底：手动提取关键字段
            choice = response.choices[0]
            return json.dumps({
                "id": getattr(response, "id", ""),
                "model": getattr(response, "model", ""),
                "content": choice.message.content,
                "role": choice.message.role,
                "finish_reason": choice.finish_reason,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                    "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                },
            })

    @staticmethod
    def _deserialize_response(json_str):
        """从 JSON 字符串重建类 OpenAI ChatCompletion 对象"""
        try:
            from openai.types.chat import ChatCompletion, ChatCompletionMessage
            from openai.types.chat.chat_completion import Choice
        except ImportError:
            # 如果 openai SDK 版本不支持类型导入，用简单 namedtuple 兜底
            return CachedLLMClient._deserialize_simple(json_str)

        data = json.loads(json_str)
        # 使用 openai SDK 的 model_validate 重建
        try:
            return ChatCompletion.model_validate(data)
        except Exception:
            return CachedLLMClient._deserialize_simple(json_str)

    @staticmethod
    def _deserialize_simple(json_str):
        """轻量级反序列化：返回类 namedtuple 对象，兼容 .choices[0].message.content 访问"""
        from collections import namedtuple
        data = json.loads(json_str)

        # 直接从 JSON 提取 content
        content = data.get("content", "")
        if not content and "choices" in data:
            content = data["choices"][0]["message"]["content"]

        Message = namedtuple("Message", ["content", "role"])
        Choice = namedtuple("Choice", ["message", "finish_reason"])
        Usage = namedtuple("Usage", ["prompt_tokens", "completion_tokens"])
        Response = namedtuple("Response", ["id", "model", "choices", "usage"])

        msg = Message(content=content, role="assistant")
        choice = Choice(message=msg, finish_reason="stop")
        usage_info = data.get("usage", {})
        usage = Usage(
            prompt_tokens=usage_info.get("prompt_tokens", 0),
            completion_tokens=usage_info.get("completion_tokens", 0),
        )
        return Response(
            id=data.get("id", ""),
            model=data.get("model", ""),
            choices=[choice],
            usage=usage,
        )


# =============================================================================
#  模块级单例（向后兼容 main.py 中的 _get_deepseek_client 模式）
# =============================================================================

_global_client = None


def get_cached_client():
    """获取全局 CachedLLMClient 单例"""
    global _global_client
    if _global_client is None:
        _global_client = CachedLLMClient()
    return _global_client
