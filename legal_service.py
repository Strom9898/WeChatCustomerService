"""Arbitration customer-service guidance, examples, and per-group case profiles."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path


PROFILE_FIELDS = {
    "procedure_type", "dispute_type", "stage", "client_goal", "parties",
    "key_facts", "evidence", "missing_information", "next_action",
    "risk_level", "needs_human",
}
LIST_FIELDS = {"parties", "key_facts", "evidence", "missing_information"}
REPLACE_LIST_FIELDS = {"missing_information"}


class ArbitrationService:
    def __init__(self, data_root: str | Path = "customer_service",
                 profile_root: str | Path = "logs/case_profiles"):
        self.data_root = Path(data_root)
        self.profile_root = Path(profile_root)
        self._lock = threading.Lock()
        self.examples = self._load_json("arbitration_examples.json")
        self.knowledge = self._load_json("arbitration_knowledge.json")

    def _load_json(self, name: str) -> list[dict]:
        try:
            data = json.loads((self.data_root / name).read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    @staticmethod
    def _tokens(text: str) -> set[str]:
        text = re.sub(r"\s+", "", str(text).lower())
        words = set(re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", text))
        words.update(text[index:index + 2] for index in range(max(0, len(text) - 1)))
        return words

    def _rank(self, query: str, items: list[dict], limit: int) -> list[dict]:
        query_tokens = self._tokens(query)
        ranked = []
        for item in items:
            searchable = " ".join(str(item.get(key, "")) for key in ("title", "category", "scenario", "keywords", "content"))
            score = len(query_tokens & self._tokens(searchable))
            for keyword in item.get("keywords", []):
                if keyword and keyword in query:
                    score += 5
            if score >= 2:
                ranked.append((score, item))
        ranked.sort(key=lambda value: value[0], reverse=True)
        return [item for _, item in ranked[:limit]]

    def retrieve_examples(self, query: str, limit: int = 3) -> list[dict]:
        return self._rank(query, self.examples, limit)

    def retrieve_knowledge(self, query: str, limit: int = 4) -> list[dict]:
        return self._rank(query, self.knowledge, limit)

    @staticmethod
    def format_examples(examples: list[dict]) -> str:
        if not examples:
            return "暂无相似范例。"
        parts = []
        for item in examples:
            replies = " / ".join(item.get("reply_segments", []))
            parts.append(f"客户：{item.get('customer', '')}\n客服分段：{replies}")
        return "\n\n".join(parts)

    @staticmethod
    def format_knowledge(items: list[dict]) -> str:
        if not items:
            return "未检索到足够的法律依据；只能追问事实或建议主办人员核实。"
        return "\n\n".join(
            f"【{item.get('title', '参考')}】{item.get('content', '')}\n来源：{item.get('source', '')}"
            for item in items
        )

    @staticmethod
    def _group_key(group_name: str) -> str:
        return hashlib.sha256(group_name.strip().encode("utf-8")).hexdigest()[:24]

    def _profile_path(self, group_name: str) -> Path:
        return self.profile_root / f"{self._group_key(group_name)}.json"

    def get_profile(self, group_name: str) -> dict:
        path = self._profile_path(group_name)
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
            return profile if isinstance(profile, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def update_profile(self, group_name: str, update: dict) -> dict:
        if not isinstance(update, dict):
            return self.get_profile(group_name)
        with self._lock:
            profile = self.get_profile(group_name)
            profile.setdefault("group_name", group_name)
            for key in PROFILE_FIELDS:
                value = update.get(key)
                if value in (None, "", [], {}):
                    continue
                if key in LIST_FIELDS:
                    incoming = value if isinstance(value, list) else [value]
                    old = [] if key in REPLACE_LIST_FIELDS else (
                        profile.get(key, []) if isinstance(profile.get(key), list) else []
                    )
                    profile[key] = list(dict.fromkeys(
                        str(item).strip()[:200] for item in old + incoming if str(item).strip()
                    ))[-30:]
                elif key == "needs_human":
                    profile[key] = bool(value)
                else:
                    profile[key] = str(value).strip()[:300]
            profile["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.profile_root.mkdir(parents=True, exist_ok=True)
            temporary = self._profile_path(group_name).with_suffix(".tmp")
            temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self._profile_path(group_name))
            return profile

    @staticmethod
    def parse_model_response(raw: str, max_segments: int = 3) -> dict:
        text = str(raw).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        data = None
        looks_structured = text.startswith("{") or "\"reply_segments\"" in text
        try:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            data = None

        if isinstance(data, dict):
            segments = data.get("reply_segments", [])
            case_update = data.get("case_update", {})
        else:
            segments = [] if looks_structured else [
                part.strip() for part in re.split(r"\n{2,}|\n", text) if part.strip()
            ]
            case_update = {}
        if isinstance(segments, str):
            segments = [segments]
        cleaned = []
        for segment in segments if isinstance(segments, list) else []:
            segment = re.sub(r"^[-*#\d.、\s]+", "", str(segment)).strip()
            segment = segment.replace("```", "")[:220]
            if segment:
                cleaned.append(segment)
        # Never send a malformed structured response: it may contain the internal case profile.
        if not cleaned and text and not looks_structured:
            cleaned = [text[:220]]
        return {"reply_segments": cleaned[:max(1, min(max_segments, 3))], "case_update": case_update}


ARBITRATION_SYSTEM_PROMPT = """你是中国大陆仲裁法律服务团队的在线接待助理。你不是裁判者，也不自称AI。

先在内部判断客户说的是商事仲裁、劳动仲裁、法院诉讼还是其他程序；不同程序不得混用规则。

说话风格：
1. 像熟悉业务的真人客服，先自然承接，例如“嗯，明白了”“那这个情况先别着急”。不要每次都说“您好”。
2. 每条消息只讲一个重点，用日常口语，不堆法律术语，不写标题、清单、Markdown或免责声明套话。
3. 优先结合前文，不重复询问客户已经说过的信息。信息不足时，一次只追问最关键的一项。
   案件档案可能保留旧信息或OCR误字；若与客户较新的明确陈述冲突，以新陈述为优先，并用一句自然问题核对，不得把两种说法擅自拼成结论。
4. 通常输出1至2条短消息；需要“承接 + 解释 + 下一步”时最多3条，每条15至100字。
5. 可以说“我先帮您理一下”“这个我再跟主办人员核实”，不要编造已经联系了某个人。

法律安全：
1. 只依据提供的法律知识和已知案情回答；没有依据时追问或转主办人员，不得猜测。
2. 不承诺胜诉、受理、赔偿金额或办理结果。
3. 涉及时效、管辖、仲裁协议效力、证据效力、撤销或执行等关键结论，案情不完整时必须提示以材料和机构要求为准，并标记needs_human。
4. 群聊中不要索要完整身份证号、银行卡号等敏感信息，可让客户私下提交或打码。
5. 劳动仲裁时效不得一概表述为“从离职日起一年”。一般规则是从知道或应当知道权利受侵害之日起一年；劳动关系存续期间拖欠劳动报酬有特别规则，劳动关系终止后应自终止之日起一年内提出。事实不完整时只提示尽快办理并交由人员核对具体起算、中断和届满日期。
6. 劳动争议可由劳动合同履行地或用人单位所在地的劳动争议仲裁委员会管辖，但不得仅凭城市名断言具体是“市仲裁委”或某区仲裁委，也不得自行承诺可以邮寄、线上申请或委托代办；应提示按当地人社部门或实际受理机构的现行要求核实。
7. 不得根据离职月份擅自倒推欠薪月份，不得说“一两项证据就够了”。没有书面劳动合同时，应提示从工资支付、社保、考勤、工牌、工作安排、聊天记录等多方面整理能够相互印证的材料，最终证明力由办案机构判断。
8. 客户只说金额、日期、地点等短句时，先结合案件档案和最近对话理解其含义，不要把已有信息再问一遍。

你必须只输出一个JSON对象，不要输出JSON以外内容：
{
  "case_update": {
    "procedure_type": "商事仲裁/劳动仲裁/法院诉讼/其他/待确认",
    "dispute_type": "纠纷类型",
    "stage": "当前阶段",
    "client_goal": "客户诉求",
    "parties": ["已知当事人关系"],
    "key_facts": ["本轮新增关键事实"],
    "evidence": ["已提到的证据"],
    "missing_information": ["仍缺少的关键信息"],
    "next_action": "建议下一步",
    "risk_level": "低/中/高",
    "needs_human": false
  },
  "reply_segments": ["第一条发给客户的话", "可选的第二条话"]
}
"""
