"""讨饭插件：群友提到钱时发收款码，并记录收到的打赏。

## 四件事

1. **讨饭** —— `beg_for_money` 工具 / `/讨饭` 命令，发一条只含收款码的合并转发。
2. **道谢** —— 讨饭后开一个窗口，窗口内有人说「打了」「转了」，或手动 `/已打钱`，
   让 Planner 自己生成一句感谢。
3. **记账** —— `record_payment` 工具 / `/打赏 15` 命令，写进本地 JSON 账本。
   群友触发时不直接落账：先私聊管理员问一句，管理员回 `/确认` 才真正记账。
4. **查账** —— `check_ledger` 工具 / `/收款` 命令，查今天、最近几天、指定日期或累计。

## 记账为什么要过一道审批

群友嘴上说「我转了 15」不代表真转了。直接落账等于让任何人往账本里写数字，
而账本是纯手工维护的，记错了只能改文件。所以群友触发的记账先挂成待确认记录
（`_approvals`），私聊管理员确认后才写盘。管理员自己触发的记账不用这道手续。

## 工具和命令共用底层

工具给 LLM 主动调，命令给人敲。两边走同一份逻辑
（`_record_and_report` / `_ledger_view`），避免行为和文案各跑一套。
只有渲染分两种：命令要明细，工具只要一两句。

## 为什么不做自动识别到账

试过两条路都不行：

- **监听 Windows 系统通知**：微信 4.x 根本不往通知中心推（通知库里 49 条全是 QQ /
  YourPhone，微信 0 条）。
- **截图 + OCR**：`PrintWindow` 对最小化窗口无效（只能抓到 160×28 的空壳），
  而且必须停在「微信支付」会话才有「今日收款」那一行。限制太多，已移除。

个人收款码没有到账回调，所以最终走「手动记账 + 口头确认」。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from maibot_sdk import (
    Command,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

#: 宿主硬性要求：config.toml 必须有 [plugin].config_version，缺失即加载失败。
SUPPORTED_CONFIG_VERSION = "2.0.0"

#: 判定「对方说打了钱」的正面词。
_POSITIVE_WORDS = (
    "打了",
    "转了",
    "付了",
    "已付",
    "到账",
    "扫了",
    "转过去",
    "打过去",
    "转啦",
    "打啦",
    "给了钱",
    "发了钱",
    "红包",
)

#: 否定模式：必须是否定词**紧贴**动作词才算「没打钱」。
#: 不能只查单个「没」——「收到没」「有没有」这种疑问句会被误伤。
_NEGATIVE_PATTERN = re.compile(
    r"没(打|转|付|给|发|扫|充)|不(打|转|付|给|发)|骗|假的|才怪|怎么可能|哪有"
)


class PluginSectionConfig(PluginConfigBase):
    """`[plugin]` 配置节。config_version 由插件维护，勿手改。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用",
        json_schema_extra={"label": "启用", "order": 0},
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置结构版本，由插件维护",
        json_schema_extra={"hidden": True, "disabled": True, "order": 99},
    )


class BegConfig(PluginConfigBase):
    __ui_label__ = "讨饭"
    __ui_icon__ = "savings"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="是否允许麦麦伸手要饭",
        json_schema_extra={
            "label": "启用讨饭",
            "hint": "关掉后工具仍注册，但调用会直接返回失败，不会发任何消息。",
            "order": 0,
        },
    )

    qr_image_path: str = Field(
        default="",
        description="收款码图片的绝对路径",
        json_schema_extra={
            "label": "收款码图片",
            "hint": "留空则工具不可用。建议放插件目录 assets/ 下，填绝对路径。",
            "placeholder": "assets/qr.png",
            "order": 1,
        },
    )

    sender_name: str = Field(
        default="麦麦",
        description="合并转发里显示的发送者昵称",
        json_schema_extra={
            "label": "转发昵称",
            "hint": "收款码以「聊天记录」形式发出时，节点上显示的昵称，一般填机器人自己的名字。",
            "order": 2,
        },
    )

    cooldown_seconds: int = Field(
        default=600,
        ge=0,
        description="同一个聊天流两次要饭之间的最小间隔（秒）",
        json_schema_extra={
            "label": "冷却时间（秒）",
            "hint": "默认 600 秒。防止同一段对话里被连续调用好几次。",
            "step": 60,
            "order": 3,
        },
    )

    daily_limit: int = Field(
        default=3,
        ge=0,
        description="每个聊天流每天最多要饭几次",
        json_schema_extra={
            "label": "每日上限",
            "hint": "默认 3 次。设 0 表示不限制（不建议）。",
            "step": 1,
            "order": 4,
        },
    )

    thanks_window_seconds: int = Field(
        default=600,
        ge=0,
        description="讨饭之后多久内算「可能有人打钱」的窗口",
        json_schema_extra={
            "label": "道谢窗口（秒）",
            "hint": "窗口内有人说「打了」「转了」才会触发道谢。设 0 表示关闭自动道谢。",
            "step": 60,
            "order": 5,
        },
    )

    thank_intent: str = Field(
        default="有人打了钱过来，去谢谢人家，别太肉麻，一两句就够",
        description="触发道谢时交给 Planner 的意图",
        json_schema_extra={
            "label": "道谢意图",
            "hint": "不是固定话术，而是交给模型生成，所以会带上她自己的语气。",
            "order": 6,
        },
    )

    admin_only: bool = Field(
        default=False,
        description="只允许管理员使用手动命令",
        json_schema_extra={
            "label": "仅管理员可用命令",
            "hint": (
                "开启后 /讨饭、/打赏、/收款 只有 admin_ids 里的人能用，"
                "普通群友发这些命令会被拒绝。"
                "模型自己决定调用工具不受影响，仍然对所有群生效。"
            ),
            "order": 7,
        },
    )

    admin_ids: list[str] = Field(
        default_factory=list,
        description="允许使用手动命令的 QQ 号",
        json_schema_extra={
            "label": "管理员 QQ 号",
            "hint": (
                "填数字即可，多个用 TOML 数组写法：[\"123456\", \"789012\"]。"
                "开启「仅管理员可用命令」后必须至少填一个，否则谁都用不了。"
            ),
            "order": 8,
        },
    )


class LedgerConfig(PluginConfigBase):
    __ui_label__ = "账本"
    __ui_icon__ = "receipt_long"
    __ui_order__ = 2

    enabled: bool = Field(
        default=True,
        description="是否记录收到的打赏",
        json_schema_extra={
            "label": "启用记账",
            "hint": "关掉后 /打赏 不记录，/收款 也查不到东西。",
            "order": 0,
        },
    )

    currency: str = Field(
        default="元",
        description="金额单位",
        json_schema_extra={"label": "货币单位", "order": 1},
    )

    recent_days: int = Field(
        default=7,
        ge=1,
        description="`/收款` 默认展示最近几天",
        json_schema_extra={
            "label": "最近几天",
            "hint": "默认 7 天。查更早的日期用 `/收款 2026-10-27`，查总计用 `/收款 全部`。",
            "step": 1,
            "order": 2,
        },
    )

    require_approval: bool = Field(
        default=True,
        description="群友触发的记账要先私聊管理员确认",
        json_schema_extra={
            "label": "记账需管理员确认",
            "hint": (
                "开启后群友说「我转了 15」不会直接进账本，而是私聊管理员问一句，"
                "管理员回 `/确认` 才真正记账并在群里道谢。"
                "管理员自己触发的记账不用确认，直接生效。"
                "关掉则谁触发都直接记。"
            ),
            "order": 3,
        },
    )

    approval_timeout_seconds: int = Field(
        default=1800,
        ge=60,
        description="待确认记账的有效期（秒）",
        json_schema_extra={
            "label": "确认有效期（秒）",
            "hint": "超时未确认就作废，免得几天前的记录突然被确认。",
            "step": 300,
            "depends_on": "require_approval",
            "order": 4,
        },
    )


class RootConfig(PluginConfigBase):
    """配置根模型。

    ⚠ `plugin` 节必须显式声明，否则宿主读不到 `plugin.config_version`。
    ⚠ 子模型必须定义在根模型**之前**：`default_factory=X` 在类体执行时求值。
    """

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    beg: BegConfig = Field(default_factory=BegConfig)
    ledger: LedgerConfig = Field(default_factory=LedgerConfig)


class BegPlugin(MaiBotPlugin):
    config_model = RootConfig

    def __init__(self) -> None:
        super().__init__()
        #: stream_id -> 上次要饭的 monotonic 时间
        self._last_sent: dict[str, float] = {}
        #: stream_id -> (日期字符串, 当日次数)
        self._daily: dict[str, tuple[str, int]] = {}
        #: stream_id -> 道谢窗口的到期 monotonic 时间
        self._pending: dict[str, float] = {}
        #: 待管理员确认的记账：编号 -> 记录
        self._approvals: dict[str, dict[str, Any]] = {}
        self._approval_seq = 0
        self._approvals_path: Path | None = None
        #: 持有后台任务强引用，避免 create_task 的返回值被 GC 掉导致道谢静默不触发
        self._bg_tasks: set[asyncio.Task] = set()
        #: 账本文件路径与内存缓存
        self._ledger_path: Path | None = None
        self._ledger: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- 生命周期

    async def on_load(self) -> None:
        cfg = self.config.beg
        if cfg.qr_image_path and Path(cfg.qr_image_path).is_file():
            self.ctx.logger.info("讨饭插件已加载，收款码=%s", cfg.qr_image_path)
        else:
            self.ctx.logger.warning("讨饭插件已加载，但收款码图片未配置或不存在，工具将不可用")

        self._ledger_path = Path(self.ctx.paths.data_dir) / "ledger.json"
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_ledger()
        total = sum(self._amount_of(r) for r in self._ledger)
        self.ctx.logger.info(
            "账本已加载：%d 笔，累计 %.2f 元，文件=%s",
            len(self._ledger),
            total,
            self._ledger_path,
        )

        # 待确认记录必须落盘：插件重载或重启后，管理员手里那个编号还得管用。
        self._approvals_path = Path(self.ctx.paths.data_dir) / "approvals.json"
        self._load_approvals()
        self._purge_approvals()
        self.ctx.logger.info(
            "待确认记账已加载：%d 条，文件=%s",
            len(self._approvals),
            self._approvals_path,
        )

    async def on_unload(self) -> None:
        # 道谢任务跑在后台，卸载后 ctx 已经失效，先取消掉再清表
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
        self._last_sent.clear()
        self._daily.clear()
        self._pending.clear()
        # ⚠ 这里别再 _save_approvals()：增删时已经即时落盘，
        # 卸载时再写一次只会在热重载时用旧内存覆盖掉新写入的记录。
        self.ctx.logger.info("讨饭插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("讨饭插件配置已热更新 version=%s", version)

    # ---------------------------------------------------------------- 工具

    @Tool(
        "beg_for_money",
        brief_description="伸手要钱：对方提到请客、打钱、投喂、发红包、赏你时调用",
        detailed_description=(
            "对方提到钱相关的事就调，别用文字推辞。调用后发一张收款码。"
            "同一段对话最多一次，别反复要。"
        ),
    )
    async def handle_beg(self, **kwargs):
        ok, message = await self._do_beg(kwargs)
        return {"success": ok, "content": message}

    @Tool(
        "check_ledger",
        brief_description="查打赏账本：有人问收到多少钱时调用",
        detailed_description=(
            "查本机账本的真实记录。拿到数字用自己的话简短说，别念明细。"
        ),
        parameters=[
            ToolParameterInfo(
                name="scope",
                param_type=ToolParamType.STRING,
                description="今天/昨天/全部/天数(如7)/日期(如2026-10-27)，不填为今天加最近几天",
                required=False,
            ),
        ],
    )
    async def handle_check_ledger(self, scope: str = "", **kwargs):
        if not self.config.plugin.enabled or not self.config.ledger.enabled:
            return {"success": False, "content": "账本功能没开，查不了。"}

        # 查账同样是看营收，跟 /收款 走同一个开关
        allowed, reason = self._check_admin(kwargs)
        if not allowed:
            return {"success": False, "content": reason}

        view = self._ledger_view(scope)
        if view is None:
            return {
                "success": False,
                "content": f"看不懂「{scope}」这个范围，能用今天、昨天、全部、7、2026-10-27。",
            }
        self.ctx.logger.debug("LLM 调用 check_ledger scope=%r", scope)
        return {"success": True, "content": self._summarize_ledger(view)}

    @Tool(
        "record_payment",
        brief_description="记一笔打赏：对方明确说出金额时调用",
        detailed_description=(
            "只在对方说出具体金额时调，猜的、开玩笑的、没金额的都不记，同一笔只记一次。"
            "返回要等管理员确认时，回一句半信半疑的话（真的假的/等我问问），别道谢也别重调。"
        ),
        parameters=[
            ToolParameterInfo(
                name="amount",
                param_type=ToolParamType.NUMBER,
                description="金额，大于 0",
                required=True,
            ),
            ToolParameterInfo(
                name="note",
                param_type=ToolParamType.STRING,
                description="备注，可留空",
                required=False,
            ),
        ],
    )
    async def handle_record_payment(
        self, amount: float = 0, note: str = "", **kwargs
    ):
        stream_id = await self._resolve_stream(kwargs)
        if not stream_id:
            return {"success": False, "content": "拿不到当前聊天流，这笔先记不了。"}

        ok, text = await self._record_and_report(
            amount,
            note,
            stream_id,
            source="工具",
            is_admin=self._is_admin_caller(kwargs),
            kwargs=kwargs,
        )
        return {"success": ok, "content": text}

    # ---------------------------------------------------------------- 命令

    @Command("beg", pattern=r"^/(讨饭|要饭|beg)$")
    async def handle_beg_command(self, **kwargs):
        stream_id = str(kwargs.get("stream_id") or "")
        allowed, reason = self._check_admin(kwargs)
        if not allowed:
            await self._reply(stream_id, reason)
            return False, reason, 1
        ok, message = await self._do_beg(kwargs)
        if not ok:
            # 成功时收款码已经发出去了，失败时才需要补一句说明
            await self._reply(stream_id, message)
        return ok, message, 1

    @Command("paid", pattern=r"^/(已打钱|打了|paid)$")
    async def handle_paid_command(self, **kwargs):
        allowed, reason = self._check_admin(kwargs)
        if not allowed:
            return False, reason, 1
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return False, "缺少 stream_id。", 1
        fired = await self._fire_thanks(stream_id, reason="手动确认已打钱")
        text = "收到，让她去道谢了。" if fired else "现在没有待确认的讨饭，或者自动道谢已关闭。"
        await self._reply(stream_id, text)
        return fired, text, 1

    @Command("record", pattern=r"^/(打赏|记账)(?:\s+(?P<amount>\d+(?:\.\d{1,2})?))?(?:\s+(?P<note>.+))?$")
    async def handle_record_command(self, **kwargs):
        stream_id = str(kwargs.get("stream_id") or "")
        matched = kwargs.get("matched_groups") or {}
        raw_amount = str(matched.get("amount") or "").strip()
        if not raw_amount:
            text = "要记多少？用法：/打赏 15 或者 /打赏 15 备注"
            await self._reply(stream_id, text)
            return False, text, 1

        ok, text = await self._record_and_report(
            raw_amount,
            str(matched.get("note") or ""),
            stream_id,
            source="命令",
            is_admin=self._is_admin_caller(kwargs),
            kwargs=kwargs,
        )
        await self._reply(stream_id, text)
        return ok, text, 1

    @Command("ledger", pattern=r"^/(收款|账本|收入)(?:\s+(?P<scope>\S+))?$")
    async def handle_ledger_command(self, **kwargs):
        stream_id = str(kwargs.get("stream_id") or "")
        allowed, reason = self._check_admin(kwargs)
        if not allowed:
            await self._reply(stream_id, reason)
            return False, reason, 1
        matched = kwargs.get("matched_groups") or {}
        scope = str(matched.get("scope") or "").strip()
        if not self.config.ledger.enabled:
            await self._reply(stream_id, "记账功能已关闭。")
            return False, "记账功能已关闭。", 1
        text = self._format_ledger(scope)
        await self._reply(stream_id, text)
        return True, text, 1

    @Command("approve", pattern=r"^/(确认|同意|approve)(?:\s+(?P<id>\d+))?$")
    async def handle_approve_command(self, **kwargs):
        return await self._resolve_approval(kwargs, approve=True)

    @Command("reject", pattern=r"^/(不记|拒绝|忽略|reject)(?:\s+(?P<id>\d+))?$")
    async def handle_reject_command(self, **kwargs):
        return await self._resolve_approval(kwargs, approve=False)

    @Command("pending", pattern=r"^/(待确认|待审|pending)$")
    async def handle_pending_command(self, **kwargs):
        stream_id = str(kwargs.get("stream_id") or "")
        if not self._is_admin_caller(kwargs):
            return False, "只有管理员能看。", 1

        self._purge_approvals()
        if not self._approvals:
            text = "没有待确认的记账。"
            await self._reply(stream_id, text)
            return True, text, 1

        unit = self.config.ledger.currency
        lines = ["待确认的记账："]
        for rec in sorted(self._approvals.values(), key=lambda r: r["created"]):
            where = rec["stream_name"] or rec["stream_id"] or "未知"
            lines.append(
                f"  #{rec['id']} {rec['amount']:.2f}{unit} — {where} / {rec['requester']}"
            )
        text = "\n".join(lines)
        await self._reply(stream_id, text)
        return True, text, 1

    async def _resolve_approval(self, kwargs: dict, approve: bool) -> tuple[bool, str, int]:
        """管理员在私聊里确认或拒绝一条待记账。"""
        stream_id = str(kwargs.get("stream_id") or "")
        if not self._is_admin_caller(kwargs):
            return False, "只有管理员能确认。", 1
        if self._is_group_message(kwargs):
            text = "确认请在私聊里说，别在群里。"
            await self._reply(stream_id, text)
            return False, text, 1

        matched = kwargs.get("matched_groups") or {}
        raw_id = str(matched.get("id") or "").strip()
        rec = self._pick_approval(raw_id)
        if rec is None:
            text = (
                f"找不到编号 {raw_id} 的待确认记录。"
                if raw_id
                else "没有待确认的记账，或者都过期了。"
            )
            await self._reply(stream_id, text)
            return False, text, 1

        unit = self.config.ledger.currency

        if not approve:
            self._approvals.pop(rec["id"], None)
            self._save_approvals()
            text = f"好，#{rec['id']} 那笔 {rec['amount']:.2f}{unit} 不记了。"
            await self._reply(stream_id, text)
            self.ctx.logger.info("记账被拒 #%s", rec["id"])
            return True, text, 1

        # 先落账再删待确认：账本写不进去的话，这条记录还得留着让管理员重试
        entry = self._record_payment(rec["amount"], rec["note"], rec["stream_id"])
        if entry is None:
            text = "账本写不进去，这笔还没记上，待确认记录给你留着。"
            await self._reply(stream_id, text)
            return False, text, 1

        self._approvals.pop(rec["id"], None)
        self._save_approvals()
        text = f"记上了 #{rec['id']}，{rec['amount']:.2f}{unit}。"
        # 道谢要发回原来的群，不是管理员私聊
        if await self._fire_thanks(
            rec["stream_id"], reason=f"确认记账 {rec['amount']:.2f}{unit}"
        ):
            text += " 已经让她在群里道谢。"
        self.ctx.logger.info("审批通过记账: %s", entry)
        await self._reply(stream_id, text)
        return True, text, 1

    # ---------------------------------------------------------------- 道谢

    @HookHandler(
        "chat.receive.after_process",
        name="detect_payment_claim",
        description="讨饭窗口内检测到「打了钱」的说法时触发道谢",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=1200,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_inbound_for_thanks(self, **kwargs):
        root = self.config
        if not root.plugin.enabled or not root.beg.enabled:
            return {}
        if root.beg.thanks_window_seconds <= 0:
            return {}

        message = kwargs.get("message") or {}
        if not isinstance(message, dict):
            return {}

        stream_id = str(message.get("session_id") or "").strip()
        if not stream_id or stream_id not in self._pending:
            return {}

        if time.monotonic() > self._pending[stream_id]:
            self._pending.pop(stream_id, None)
            return {}

        text = str(message.get("processed_plain_text") or "").strip()
        if not text or len(text) > 60:
            return {}
        if not any(word in text for word in _POSITIVE_WORDS):
            return {}
        if _NEGATIVE_PATTERN.search(text):
            return {}

        task = asyncio.create_task(
            self._fire_thanks(stream_id, reason=f"群友说：{text[:30]}")
        )
        # 不持引用的话任务可能在执行前被 GC，道谢就静默丢了
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return {}

    async def _fire_thanks(self, stream_id: str, reason: str) -> bool:
        """让 Planner 主动说一句感谢，然后关掉窗口。"""
        if stream_id not in self._pending:
            return False
        self._pending.pop(stream_id, None)
        intent = str(self.config.beg.thank_intent or "").strip() or "有人打了钱，去道谢"
        try:
            await self.ctx.maisaka.trigger_proactive(stream_id, intent)
            self.ctx.logger.info("已触发道谢 stream=%s 原因=%s", stream_id, reason)
            return True
        except Exception as exc:
            self.ctx.logger.error("触发道谢失败: %s", exc)
            return False

    # ---------------------------------------------------------------- 讨饭

    def _caller_id(self, kwargs: dict) -> str:
        """挖出调用者 QQ 号，只认宿主注入的位置。挖不到返回空串。

        两条路径的实际载荷不一样：
        - 命令：`kwargs["message"]["message_info"]["user_info"]["user_id"]`
        - 工具：Runner 直接注入 `kwargs["user_id"]`（实测键为
          `['chat_id', 'platform', 'user_id']`，没有 message）

        工具声明的参数只有 amount / stream_id / note，模型无法通过 JSON schema
        传别的键，所以 `kwargs["user_id"]` 不可能是伪造的。除此之外的
        `sender_id` / `caller_id` 之类来源没有保证，一律不认。
        """
        message = kwargs.get("message")
        if isinstance(message, dict):
            info = message.get("message_info")
            if isinstance(info, dict):
                user = info.get("user_info") or {}
                if isinstance(user, dict):
                    uid = str(user.get("user_id") or "").strip()
                    if uid:
                        return uid

            sender = message.get("sender") or {}
            if isinstance(sender, dict):
                uid = str(sender.get("user_id") or "").strip()
                if uid:
                    return uid

            uid = str(message.get("user_id") or "").strip()
            if uid:
                return uid

        # ⚠ 工具路径不采信 kwargs["user_id"]：宿主只在键缺失时才补上下文，
        # 模型自己带一个管理员的 user_id 就能压过注入值、让审批形同虚设。
        # 插件侧无法区分这个值是注入的还是模型塞的，所以一律按「认不出」处理，
        # 工具触发的记账会走审批（fail-closed）。管理员想直接记就用 /打赏 命令。
        if str(kwargs.get("user_id") or "").strip():
            self.ctx.logger.warning(
                "工具参数里出现 user_id，无法确认来源，按非管理员处理"
            )
        return ""

    def _admin_ids(self) -> list[str]:
        return [str(x).strip() for x in (self.config.beg.admin_ids or []) if str(x).strip()]

    async def _fetch_stream(self, fetch, arg: str, label: str) -> str:
        """调一次反查，拿回 stream_id。查不到或出错返回空串。"""
        if not arg:
            return ""
        try:
            session = await fetch(arg)
        except Exception as exc:
            self.ctx.logger.warning("按%s反查聊天流失败(%s): %s", label, arg, exc)
            return ""
        sid = ""
        if isinstance(session, dict):
            sid = str(
                session.get("stream_id") or session.get("session_id") or ""
            ).strip()
        self.ctx.logger.debug("反查流 %s=%r -> %r", label, arg, sid)
        return sid

    async def _lookup_stream(self, kwargs: dict) -> str:
        """用注入的 chat_id / user_id 反查宿主的流。查不到返回空串。

        ⚠ 群聊里 `user_id` 是**发言人**，拿它查会查到那个人的私聊流，
        把本该发到群里的消息发到私聊去。所以纯数字的 chat_id（群号）只按群查，
        绝不退回 user_id。
        """
        chat_id = str(kwargs.get("chat_id") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip()

        if chat_id.isdigit():
            # 少数平台直接拿 QQ 号当会话 ID，这种情况按私聊查
            if user_id and chat_id == user_id:
                return await self._fetch_stream(
                    self.ctx.chat.get_stream_by_user_id, user_id, "私聊"
                )
            return await self._fetch_stream(
                self.ctx.chat.get_stream_by_group_id, chat_id, "群"
            )

        # 非数字：私聊或内部会话。先按群试（有些平台的 chat_id 直接就是流 ID），
        # 查不到再按 user_id 查私聊流。
        sid = await self._fetch_stream(
            self.ctx.chat.get_stream_by_group_id, chat_id, "群"
        )
        if sid:
            return sid
        return await self._fetch_stream(
            self.ctx.chat.get_stream_by_user_id, user_id, "私聊"
        )

    async def _resolve_stream(self, kwargs: dict) -> str:
        """找出这次操作该作用在哪个聊天流。拿不到可信来源就返回空串。

        优先级：命令路径的 `message.session_id` → 反查结果 → 非数字 chat_id 兜底
        → 注入的 `stream_id`。

        宿主构造工具 payload 时先把模型传的 arguments 原样放进去、仅当键缺失或为空
        才补上下文（`component_query._build_tool_invocation_payload`），所以
        `stream_id` 未必是可信的当前流——QQ 私聊实测给的是机器人账号号，直接拿去
        发会报「未找到聊天流」。因此优先用 `chat_id` / `user_id` 反查出来的真流。
        """
        message = kwargs.get("message")
        if isinstance(message, dict):
            sid = str(message.get("session_id") or "").strip()
            if sid:
                return sid

        injected = str(kwargs.get("stream_id") or "").strip()
        resolved = await self._lookup_stream(kwargs)

        # 以反查结果为准：私聊场景下宿主注入的 stream_id 可能就是账号号，
        # 直接拿去发会报「未找到聊天流」；反查拿到的才是真实流。
        if resolved:
            if injected and injected != resolved:
                self.ctx.logger.warning(
                    "注入的 stream_id(%s…) 与反查(%s…) 不一致，以反查为准",
                    injected[:8],
                    resolved[:8],
                )
            return resolved

        # 兜底：chat_id 在 QQ 私聊里直接就是流 ID，而 stream_id 反而是机器人账号号。
        chat_id = str(kwargs.get("chat_id") or "").strip()
        if chat_id and not chat_id.isdigit():
            self.ctx.logger.debug("反查全失败，改用 chat_id 当流 ID: %r", chat_id)
            return chat_id

        return injected

    def _is_admin_caller(self, kwargs: dict) -> bool:
        """只看名单，不看开关。用来区分「管理员自己记」和「群友要审批」。"""
        allowed = set(self._admin_ids())
        caller = self._caller_id(kwargs)
        return bool(caller) and caller in allowed

    def _caller_info(self, kwargs: dict) -> tuple[str, str, str]:
        """返回 (user_id, 昵称, 群名)。挖不到的给空串。"""
        name = ""
        group = ""
        message = kwargs.get("message")
        if isinstance(message, dict):
            info = message.get("message_info")
            if isinstance(info, dict):
                user = info.get("user_info") or {}
                if isinstance(user, dict):
                    name = str(
                        user.get("user_nickname") or user.get("user_cardname") or ""
                    ).strip()
                gi = info.get("group_info") or {}
                if isinstance(gi, dict):
                    group = str(gi.get("group_name") or "").strip()

        # 工具路径没有 message，只能拿注入的 chat_id 当群标识，至少让管理员知道是哪个群。
        # ⚠ 只有纯数字才可能是群号：私聊的 chat_id 是一串 UUID，拿它拼「群 xxx」
        # 会让管理员以为这笔发生在某个群里。
        if not group:
            chat_id = str(kwargs.get("chat_id") or "").strip()
            uid = str(kwargs.get("user_id") or "").strip()
            if chat_id.isdigit() and chat_id != uid:
                group = f"群 {chat_id}"

        return self._caller_id(kwargs), name, group

    @staticmethod
    def _is_group_message(kwargs: dict) -> bool:
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return False
        info = message.get("message_info")
        if isinstance(info, dict) and info.get("group_info"):
            return True
        return bool(message.get("group_id"))

    def _check_admin(self, kwargs: dict) -> tuple[bool, str]:
        """讨饭/道谢/查账的权限检查，看 `beg.admin_only`。"""
        if not self.config.beg.admin_only:
            return True, ""

        allowed = set(self._admin_ids())
        if not allowed:
            return False, "还没配置管理员 QQ 号，谁都用不了。"

        caller = self._caller_id(kwargs)
        if caller and caller in allowed:
            return True, ""
        if not caller:
            self.ctx.logger.warning("认不出调用者，已拒绝。kwargs keys=%s", list(kwargs))
            return False, "认不出是谁在操作，这个动作只给管理员。"
        return False, "这个动作只有管理员能做。"

    async def _reply(self, stream_id: str, text: str) -> None:
        """命令的返回值不会自动发到聊天流（bot.py:665 只存库记日志），必须自己发。"""
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            self.ctx.logger.error("发送命令响应失败: %s", exc)

    def _prune_state(self, now: float, today: str) -> None:
        """清掉已经失效的流状态，避免字典跟着聊天流数量一直长。

        - `_last_sent`：冷却已过的没用了
        - `_daily`：日期翻篇的计数下次会被重置，旧条目没用了
        - `_pending`：道谢窗口已经关掉的
        """
        cooldown = max(0, self.config.beg.cooldown_seconds)
        for key in [k for k, t in self._last_sent.items() if now - t >= cooldown]:
            self._last_sent.pop(key, None)
        for key in [k for k, (d, _) in self._daily.items() if d != today]:
            self._daily.pop(key, None)
        for key in [k for k, deadline in self._pending.items() if now > deadline]:
            self._pending.pop(key, None)

    async def _do_beg(self, kwargs: dict) -> tuple[bool, str]:
        """真正执行一次讨饭，Tool 和 Command 共用。"""
        root = self.config
        if not root.plugin.enabled or not root.beg.enabled:
            return False, "讨饭功能已关闭。"

        stream_id = await self._resolve_stream(kwargs)
        if not stream_id:
            return False, "拿不到当前聊天流，这次不发。"

        qr_path = str(root.beg.qr_image_path or "").strip()
        if not qr_path:
            return False, "还没配置收款码图片，发不了。"
        path = Path(qr_path)
        if not path.is_file():
            self.ctx.logger.warning("收款码图片不存在: %s", qr_path)
            return False, "收款码图片找不到，发不了。"

        now = time.monotonic()
        last = self._last_sent.get(stream_id)
        if last is not None and now - last < root.beg.cooldown_seconds:
            left = int(root.beg.cooldown_seconds - (now - last))
            return False, f"刚伸过手了，{left} 秒内别再要。"

        today = date.today().isoformat()
        day, count = self._daily.get(stream_id, (today, 0))
        if day != today:
            day, count = today, 0
        if root.beg.daily_limit > 0 and count >= root.beg.daily_limit:
            return False, "今天要够数了，明天再来。"

        sender = str(root.beg.sender_name or "").strip() or "麦麦"
        try:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        except Exception as exc:
            self.ctx.logger.error("读取收款码失败: %s", exc)
            return False, "收款码读不出来，发不了。"

        # 合并转发里**只放图片**：讨饭的话由模型在正常回复里说，转发里再重复会显得啰嗦。
        delivered = False
        try:
            await self.ctx.send.forward(
                [
                    {
                        "user_id": "",
                        "nickname": sender,
                        "segments": [{"type": "image", "content": b64}],
                    },
                ],
                stream_id,
            )
            delivered = True
        except Exception as exc:
            self.ctx.logger.warning("合并转发失败，退回直接发图: %s", exc)

        if not delivered:
            try:
                await self.ctx.send.image(b64, stream_id)
                delivered = True
            except Exception as exc:
                self.ctx.logger.error("发送收款码失败: %s", exc)
                return False, "收款码没发出去。"

        self._last_sent[stream_id] = now
        self._daily[stream_id] = (day, count + 1)
        if root.beg.thanks_window_seconds > 0:
            self._pending[stream_id] = now + root.beg.thanks_window_seconds
        self._prune_state(now, day)
        self.ctx.logger.info("已发送收款码 stream=%s 今日第 %d 次", stream_id, count + 1)
        return True, f"已经伸手要过了，收款码发出去了（今日第 {count + 1} 次）。"

    # ---------------------------------------------------------------- 账本

    def _load_ledger(self) -> None:
        if self._ledger_path is None or not self._ledger_path.is_file():
            self._ledger = []
            return
        try:
            data = json.loads(self._ledger_path.read_text(encoding="utf-8"))
            records = data.get("records") if isinstance(data, dict) else data
            self._ledger = [r for r in (records or []) if isinstance(r, dict)]
        except Exception as exc:
            self.ctx.logger.error("账本读取失败，按空账本处理: %s", exc)
            self._ledger = []

    def _write_json(self, path: Path | None, payload: Any, label: str) -> bool:
        """原子写：先写同目录临时文件，再 os.replace 换上去。返回是否写成功。

        账本是纯手工维护的唯一副本，直接覆盖时如果写到一半进程没了，
        留下的就是半个 JSON，那份数据真的救不回来。
        """
        if path is None:
            return False
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, path)
            return True
        except Exception as exc:
            self.ctx.logger.error("%s写入失败: %s", label, exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def _save_ledger(self) -> bool:
        return self._write_json(
            self._ledger_path, {"version": 1, "records": self._ledger}, "账本"
        )

    def _record_payment(
        self, amount: float, note: str, stream_id: str
    ) -> dict[str, Any] | None:
        """记一笔并落盘。写盘失败返回 None，内存里也一并退回。"""
        now = datetime.now()
        entry = {
            "date": now.date().isoformat(),
            "time": now.strftime("%H:%M:%S"),
            "amount": round(float(amount), 2),
            "note": note[:40],
            "stream": stream_id,
        }
        self._ledger.append(entry)
        if not self._save_ledger():
            # 没写进磁盘就别留在内存里，否则这一轮统计会多出一笔、
            # 重启后又消失，等于跟用户说了句假话。
            self._ledger.pop()
            return None
        return entry

    @staticmethod
    def _amount_of(row: dict[str, Any]) -> float:
        """账本金额容错：手工改坏的一行按 0 算，别让整个统计崩掉。"""
        try:
            return float(row.get("amount") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _day_total(self, day: str) -> tuple[float, int]:
        rows = [r for r in self._ledger if str(r.get("date")) == day]
        return round(sum(self._amount_of(r) for r in rows), 2), len(rows)

    def _bucket_by_day(
        self, start: date, span: int
    ) -> tuple[list[tuple[str, int, float]], float, int]:
        """按天聚合最近 `span` 天，账本只扫一遍。

        原来是每一天各调一次 `_day_total`，等于 O(天数 × 笔数)；
        这里改成一次扫描 + 一次按天回填，顺序仍是今天在前。
        """
        if span <= 0:
            return [], 0.0, 0

        wanted = {(start - timedelta(days=o)).isoformat() for o in range(span)}
        agg: dict[str, list[float]] = {}
        for row in self._ledger:
            key = str(row.get("date") or "")
            if key not in wanted:
                continue
            slot = agg.setdefault(key, [0, 0.0])
            slot[0] += 1
            slot[1] += self._amount_of(row)

        buckets: list[tuple[str, int, float]] = []
        total = 0.0
        count = 0
        for offset in range(span):
            key = (start - timedelta(days=offset)).isoformat()
            slot = agg.get(key)
            if not slot:
                continue
            buckets.append((key, int(slot[0]), round(slot[1], 2)))
            total += slot[1]
            count += int(slot[0])
        return buckets, round(total, 2), count

    async def _record_and_report(
        self,
        amount: Any,
        note: str,
        stream_id: str,
        source: str,
        *,
        is_admin: bool = False,
        kwargs: dict | None = None,
    ) -> tuple[bool, str]:
        """记一笔并回一句话，命令和工具共用。

        非管理员触发且开了审批时，不直接落账，而是生成一条待确认记录并私聊管理员。
        """
        root = self.config
        if not root.plugin.enabled or not root.ledger.enabled:
            return False, "记账功能已关闭。"

        try:
            value = float(amount)
        except (TypeError, ValueError):
            return False, "金额不是数字，记不了。"
        if value <= 0:
            return False, "金额要大于 0。"
        if value > 10000:
            return False, "金额太大了，先确认一下再记。"

        unit = root.ledger.currency

        # 群友触发 → 先问管理员；管理员自己触发就直接记。
        if root.ledger.require_approval and not is_admin:
            rec = await self._create_approval(value, note, stream_id, kwargs or {})
            if rec is None:
                return False, "联系不上管理员，这笔先记不了。"
            return True, (
                f"收到 {value:.2f}{unit} 的说法，已经报给管理员了，等他点头才算数"
                f"（编号 {rec['id']}）。"
            )

        entry = self._record_payment(value, str(note or "").strip(), str(stream_id or ""))
        if entry is None:
            return False, "账本写不进去，这笔没记上。"
        today_total, today_count = self._day_total(date.today().isoformat())
        text = f"记下了 {value:.2f}{unit}，今天 {today_count} 笔共 {today_total:.2f}{unit}。"
        # 正好在讨饭窗口内就顺手道谢
        if await self._fire_thanks(stream_id, reason=f"记账 {value:.2f}{unit}"):
            text += " 已经让她去道谢了。"
        self.ctx.logger.info("%s记账: %s", source, entry)
        return True, text

    # ---------------------------------------------------------------- 记账审批

    async def _dm_admins(self, text: str) -> bool:
        """把一段话私聊给所有管理员。至少送到一个人就算成功。"""
        ids = self._admin_ids()
        if not ids:
            return False
        # 正文里有群名和昵称，不写进日志，免得日志被分享出去时把群信息带出去
        self.ctx.logger.debug("准备私聊 %d 个管理员", len(ids))

        delivered = 0
        for uid in ids:
            try:
                session = await self.ctx.chat.get_stream_by_user_id(uid)
            except Exception as exc:
                self.ctx.logger.warning("查管理员 %s 的私聊流失败: %s", uid, exc)
                continue
            sid = ""
            if isinstance(session, dict):
                sid = str(session.get("stream_id") or session.get("session_id") or "").strip()
            if not sid:
                self.ctx.logger.warning("管理员 %s 没有可用的私聊流", uid)
                continue
            try:
                await self.ctx.send.text(text, sid)
                delivered += 1
            except Exception as exc:
                self.ctx.logger.warning("私聊管理员 %s 失败: %s", uid, exc)
        return delivered > 0

    async def _create_approval(
        self, amount: float, note: str, stream_id: str, kwargs: dict
    ) -> dict[str, Any] | None:
        """生成待确认记录并私聊管理员。送不到就返回 None，不落账。"""
        _, nickname, group_name = self._caller_info(kwargs)
        self._approval_seq += 1
        rec: dict[str, Any] = {
            "id": str(self._approval_seq),
            "amount": round(float(amount), 2),
            "note": str(note or "").strip()[:40],
            "stream_id": str(stream_id or ""),
            "stream_name": group_name,
            "requester": nickname or self._caller_id(kwargs) or "有人",
            # 用墙上时间，不能用 monotonic：要跨重载/重启判断有效期
            "created": time.time(),
        }

        unit = self.config.ledger.currency
        where = group_name or "当前会话"
        lines = [
            f"【记账确认 #{rec['id']}】",
            # 中文里别在「群里」中间断空格
            f"{where}里 {rec['requester']} 说打了 {rec['amount']:.2f}{unit}",
        ]
        if rec["note"]:
            lines.append(f"备注：{rec['note']}")
        # ⚠ 别写「/拒绝」：QQ 会把这个词转成表情，管理员收到的是图标，没法照做。
        lines.append(f"回 /确认 {rec['id']} 记账并在群里道谢")
        lines.append(f"回 /不记 {rec['id']} 就忽略这笔")

        if not await self._dm_admins("\n".join(lines)):
            return None

        self._approvals[rec["id"]] = rec
        self._save_approvals()
        # 昵称和群名只出现在私聊正文里，不进日志：日志有可能被贴到外面去
        self.ctx.logger.info(
            "记账待确认 #%s: %.2f%s stream=%s",
            rec["id"],
            rec["amount"],
            unit,
            rec["stream_id"],
        )
        return rec

    def _load_approvals(self) -> None:
        if self._approvals_path is None or not self._approvals_path.is_file():
            return
        try:
            data = json.loads(self._approvals_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.ctx.logger.error("待确认记录读取失败，按空处理: %s", exc)
            self._approvals = {}
            return

        # 顶层不是对象（手工改坏、或者被别的工具重写过）就当空的，别让 on_load 崩掉
        if not isinstance(data, dict):
            self.ctx.logger.warning("待确认记录顶层不是对象，按空处理")
            data = {}

        # seq 单独容错：它坏了不该连累下面已经有效的记录
        try:
            self._approval_seq = int(data.get("seq") or 0)
        except (TypeError, ValueError):
            self._approval_seq = 0

        items = data.get("items")
        if not isinstance(items, dict):
            items = {}

        # 文件可能被手工改过、也可能是旧版本留下的：逐条补默认值，
        # 免得 /待确认、/确认 里下标访问直接抛 KeyError 把命令打崩。
        clean: dict[str, dict[str, Any]] = {}
        dropped = 0
        for key, raw in items.items():
            if not isinstance(raw, dict):
                dropped += 1
                continue
            try:
                clean[str(key)] = {
                    "id": str(raw.get("id") or key),
                    "amount": float(raw.get("amount") or 0),
                    "note": str(raw.get("note") or ""),
                    "stream_id": str(raw.get("stream_id") or ""),
                    "stream_name": str(raw.get("stream_name") or ""),
                    "requester": str(raw.get("requester") or "有人"),
                    "created": float(raw.get("created") or 0),
                }
            except (TypeError, ValueError):
                dropped += 1
        self._approvals = clean

        # 编号必须大于现存所有记录，否则新记录会跟旧编号撞上、互相顶掉
        for key in clean:
            if key.isdigit():
                self._approval_seq = max(self._approval_seq, int(key))

        if dropped:
            self.ctx.logger.warning("待确认记录里有 %d 条格式不对，已丢弃", dropped)

    def _save_approvals(self) -> None:
        self._write_json(
            self._approvals_path,
            {"seq": self._approval_seq, "items": self._approvals},
            "待确认记录",
        )

    def _purge_approvals(self) -> bool:
        """丢掉超时的待确认记录。返回是否有变动。"""
        ttl = self.config.ledger.approval_timeout_seconds
        now = time.time()
        stale = [k for k, v in self._approvals.items() if now - float(v.get("created") or 0) > ttl]
        for key in stale:
            self._approvals.pop(key, None)
        if stale:
            self._save_approvals()
            self.ctx.logger.info("清理超时待确认记录: %s", stale)
        return bool(stale)

    def _pick_approval(self, raw_id: str) -> dict[str, Any] | None:
        self._purge_approvals()
        if not self._approvals:
            return None
        if raw_id:
            return self._approvals.get(raw_id)
        # 没给编号就取最近一条，管理员直接回 /确认 也能用
        return max(self._approvals.values(), key=lambda r: r["created"])

    def _parse_scope(self, scope: str) -> tuple[str, Any]:
        """把各种范围写法归一成 (kind, value)。kind 为 bad 表示看不懂。

        命令和工具共用这一份，避免 `/收款` 认的写法和 LLM 传的写法对不上。
        """
        raw = str(scope or "").strip()
        if not raw:
            return "default", None
        low = raw.lower()
        if low in ("全部", "总", "总计", "all", "total"):
            return "all", None
        if low in ("今天", "今日", "today"):
            return "day", date.today().isoformat()
        if low in ("昨天", "昨日", "yesterday"):
            return "day", (date.today() - timedelta(days=1)).isoformat()
        if raw.isdigit():
            return "days", max(1, min(int(raw), 3650))
        key = f"{date.today().year}-{raw}" if re.fullmatch(r"\d{1,2}-\d{1,2}", raw) else raw
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
            return "day", key
        return "bad", raw

    def _ledger_view(self, scope: str) -> dict[str, Any] | None:
        """把 scope 解析成一份可渲染的视图。返回 None 表示 scope 看不懂。"""
        kind, value = self._parse_scope(scope)
        if kind == "bad":
            return None

        today = date.today()
        grand = round(sum(self._amount_of(r) for r in self._ledger), 2)
        view: dict[str, Any] = {
            "kind": kind,
            "unit": self.config.ledger.currency,
            "label": "",
            "total": 0.0,
            "count": 0,
            "rows": [],
            "extra": 0,
            "buckets": [],
            "grand": grand,
            "first": "",
        }

        if kind == "all":
            view.update(
                label="累计",
                total=grand,
                count=len(self._ledger),
                first=min((str(r.get("date")) for r in self._ledger), default=""),
            )
            return view

        if kind == "day":
            rows = [r for r in self._ledger if str(r.get("date")) == value]
            view.update(
                label=str(value),
                total=round(sum(self._amount_of(r) for r in rows), 2),
                count=len(rows),
                rows=rows[-10:],
                extra=max(0, len(rows) - 10),
            )
            return view

        span = int(value) if kind == "days" else self.config.ledger.recent_days
        buckets, span_total, span_count = self._bucket_by_day(today, span)

        if kind == "days":
            view.update(
                label=f"最近 {span} 天",
                total=round(span_total, 2),
                count=span_count,
                buckets=buckets,
            )
            return view

        today_total, today_count = self._day_total(today.isoformat())
        view.update(
            label="今天",
            total=today_total,
            count=today_count,
            buckets=buckets,
            span_label=f"最近 {span} 天",
            span_total=round(span_total, 2),
            span_count=span_count,
        )
        return view

    def _format_ledger(self, scope: str) -> str:
        """命令用的详细版：带明细行。"""
        view = self._ledger_view(scope)
        if view is None:
            return "看不懂这个范围，用 `/收款 2026-10-27`、`/收款 7` 或者 `/收款 全部`。"
        if not self._ledger:
            return "账本还是空的。"

        unit = view["unit"]
        kind = view["kind"]

        if kind == "all":
            return (
                f"累计 {view['total']:.2f}{unit}，"
                f"共 {view['count']} 笔，最早 {view['first']}。"
            )
        if kind == "default" and view["count"] == 0 and not view["buckets"]:
            return f"今天没有记录，账本累计 {view['grand']:.2f}{unit}。"
        if kind == "day" and view["count"] == 0:
            return f"{view['label']} 没有记录。"

        lines = [f"{view['label']} {view['count']} 笔，共 {view['total']:.2f}{unit}"]
        for row in view["rows"]:
            note = f" {row.get('note')}" if row.get("note") else ""
            lines.append(
                f"  {row.get('time')} {self._amount_of(row):.2f}{unit}{note}"
            )
        if view["extra"]:
            lines.append(f"  ……另有 {view['extra']} 笔")

        if kind == "default":
            if view["buckets"]:
                lines.append(
                    f"最近 {self.config.ledger.recent_days} 天 "
                    f"{view['span_count']} 笔，共 {view['span_total']:.2f}{unit}"
                )
                for key, count, total in view["buckets"]:
                    lines.append(f"  {key} {count} 笔 {total:.2f}{unit}")
            lines.append(f"累计 {view['grand']:.2f}{unit}（{len(self._ledger)} 笔）")
        elif kind == "days":
            for key, count, total in view["buckets"]:
                lines.append(f"  {key} {count} 笔 {total:.2f}{unit}")

        return "\n".join(lines)

    def _summarize_ledger(self, view: dict[str, Any]) -> str:
        """工具用的紧凑版：一两句，方便 LLM 直接转述。"""
        unit = view["unit"]
        if not self._ledger:
            return "账本还是空的，一笔都没记。"
        if view["kind"] == "all":
            return (
                f"账本累计 {view['total']:.2f}{unit}，"
                f"共 {view['count']} 笔，最早 {view['first']}。"
            )
        if view["count"] == 0 and not view["buckets"]:
            return f"{view['label']}没有记录，账本累计 {view['grand']:.2f}{unit}。"

        parts = [f"{view['label']} {view['count']} 笔，共 {view['total']:.2f}{unit}"]
        if view["buckets"]:
            days = "、".join(
                f"{key[5:]} {total:.2f}{unit}" for key, _, total in view["buckets"][:5]
            )
            parts.append(f"有记录的日子：{days}")
        if view["kind"] == "default":
            parts.append(f"累计 {view['grand']:.2f}{unit}（{len(self._ledger)} 笔）")
        return "；".join(parts) + "。"


def create_plugin() -> BegPlugin:
    return BegPlugin()
