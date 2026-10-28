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
import math
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
        #: 正在触发道谢的流，防止同一条窗口被两条「打了」各触发一次
        self._thanks_inflight: set[str] = set()
        #: stream_id -> 上次提交待确认记账的 monotonic 时间，用来节流
        self._last_approval: dict[str, float] = {}
        #: 账本文件路径与内存缓存
        self._ledger_path: Path | None = None
        self._ledger: list[dict[str, Any]] = []
        #: 账本解析失败时为 True，此时禁止写盘，免得用空账本覆盖掉用户的历史记录
        self._ledger_broken = False

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
        self._thanks_inflight.clear()
        self._last_approval.clear()
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
                "content": f"看不懂「{str(scope)[:20]}」这个范围，能用今天、昨天、全部、7、2026-10-27。",
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
        stream_id = self._resolve_stream(kwargs)
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

    @Command("record", pattern=r"^/(打赏|记账)(?:\s+(?P<amount>\d+(?:\.\d+)?))?(?:\s+(?P<note>.+))?$")
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
        # 营收是敏感数据：开了 admin_only 就说明不想公开，那群里发也不给看
        if self.config.beg.admin_only and self._is_group_message(kwargs):
            text = "营收明细请私聊查。"
            await self._reply(stream_id, text)
            return False, text, 1
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
            text = "只有管理员能看。"
            await self._reply(stream_id, text)
            return False, text, 1
        # 列表里有别的群的群名、昵称和金额，群聊里发等于公开
        if self._is_group_message(kwargs):
            text = "待确认列表请私聊查。"
            await self._reply(stream_id, text)
            return False, text, 1

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
            # 私聊里不回一句的话，非管理员发 /确认 会完全没反应
            text = "只有管理员能确认。"
            await self._reply(stream_id, text)
            return False, text, 1
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
        if not self._save_approvals():
            # 审批记录没删掉：磁盘上它还在，重启后会被重新加载，管理员再点一次
            # 就会把同一笔记两遍。把刚记的这笔撤回去，保证两边一致。
            self._approvals[rec["id"]] = rec
            try:
                self._ledger.remove(entry)
            except ValueError:
                pass
            self._save_ledger()
            text = "待确认记录删不掉（写盘失败），这笔先不记，检查一下磁盘。"
            self.ctx.logger.error("删除待确认 #%s 落盘失败，已回滚账本条目", rec["id"])
            await self._reply(stream_id, text)
            return False, text, 1

        text = f"记上了 #{rec['id']}（{rec['stream_name'] or '当前会话'}），{rec['amount']:.2f}{unit}。"
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
            # 不带群友原话：日志有可能被贴到外面去
            self._fire_thanks(stream_id, reason="窗口内检测到打款说法")
        )
        # 不持引用的话任务可能在执行前被 GC，道谢就静默丢了
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return {}

    async def _fire_thanks(self, stream_id: str, reason: str) -> bool:
        """让 Planner 主动说一句感谢。成功才关窗口，失败留着下次再试。"""
        if stream_id not in self._pending:
            return False
        # 同一个流同时只允许一次在途：窗口要等成功才 pop，这中间另一条
        # 「打了」也会通过上面的判断，没有这个集合就会道谢两遍。
        if stream_id in self._thanks_inflight:
            return False
        self._thanks_inflight.add(stream_id)
        intent = str(self.config.beg.thank_intent or "").strip() or "有人打了钱，去道谢"
        try:
            result = await self.ctx.maisaka.trigger_proactive(stream_id, intent)
        except Exception as exc:
            self.ctx.logger.error("触发道谢失败: %s", exc)
            return False
        finally:
            self._thanks_inflight.discard(stream_id)

        # 宿主拒绝时返回 {"success": False, "error": ...}，不抛异常。
        # 这时候窗口不能关，否则「已经让她去道谢」就是句假话。
        if isinstance(result, dict) and result.get("success") is False:
            self.ctx.logger.warning(
                "道谢没触发成功: %s", str(result.get("error") or result)[:200]
            )
            return False

        self._pending.pop(stream_id, None)
        self.ctx.logger.info("已触发道谢 stream=%s 原因=%s", stream_id, reason)
        return True

    # ---------------------------------------------------------------- 讨饭

    def _caller_id(self, kwargs: dict) -> str:
        """挖出调用者 QQ 号。只认命令路径里宿主注入的位置，挖不到返回空串。

        ⚠ 判别路径靠 `matched_groups` 键：命令路径一定带（宿主
        `component_query.py:518-526` 固定注入，正则没有命名组时是空 dict），
        工具路径没有这个键。

        工具路径下的 `message` / `user_id` 都只可能是模型自己带的：
        宿主 `_build_tool_context_payload` 只注入 stream_id / chat_id /
        group_id / user_id / platform，不含 message；而 `payload =
        dict(invocation.arguments)` 让模型传的同名键压过宿主值。
        所以工具路径一律按「认不出」处理，记账走审批（fail-closed）。
        管理员想直接记就用 /打赏 命令。
        """
        if "matched_groups" not in kwargs:
            if kwargs.get("message") or str(kwargs.get("user_id") or "").strip():
                self.ctx.logger.warning(
                    "工具参数里出现身份字段，无法确认来源，按非管理员处理"
                )
            return ""

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

        return ""

    def _admin_ids(self) -> list[str]:
        return [str(x).strip() for x in (self.config.beg.admin_ids or []) if str(x).strip()]

    @staticmethod
    def _resolve_stream(kwargs: dict) -> str:
        """这次操作该作用在哪个聊天流。拿不到返回空串。

        宿主在 `component_query._build_tool_context_payload`（747-750 行）里把
        `stream_id` 和 `chat_id` 设成同一个值 `context.stream_id or context.session_id`；
        命令路径（518-526 行）的 `stream_id` 同样是 `message.session_id`。
        两条路径拿到的都是当前会话 ID，可以直接交给 `ctx.send.*`。

        ⚠ 绝不能拿 `chat_id` 去 `get_stream_by_group_id` 反查：群聊里它是一串
        32 位 session_id 而不是群号，反查必然失败；失败后再退回
        `get_stream_by_user_id`（那里面是发言人）就会把收款码发到发言人的私聊去。
        真实群号在 `group_id` 里，但那个键模型也能塞，所以干脆不做任何反查。
        """
        for key in ("stream_id", "chat_id"):
            value = str(kwargs.get(key) or "").strip()
            if value:
                return value
        return ""

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
        # 只有命令路径的 message 可信（工具路径没有 matched_groups 键，
        # 那里的 message 是模型自己塞的，昵称和群名都能编）
        if isinstance(message, dict) and "matched_groups" in kwargs:
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

        # 工具路径没有 message，用宿主注入的 group_id（真实群号）当群标识。
        # ⚠ 别用 chat_id：那是会话的 session_id（32 位十六进制），不是群号，
        # 拿它拼「群 xxx」会让管理员以为这笔发生在某个奇怪的群里。
        if not group:
            group_id = str(kwargs.get("group_id") or "").strip()
            if group_id.isdigit():
                group = f"群 {group_id}"

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
            ok = bool(await self.ctx.send.text(text, stream_id))
        except Exception as exc:
            self.ctx.logger.error("发送命令响应失败: %s", exc)
            return
        if not ok:
            self.ctx.logger.warning("命令响应没发出去，宿主返回失败")

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
        for key in [k for k, t in self._last_approval.items() if now - t >= 60]:
            self._last_approval.pop(key, None)

    async def _do_beg(self, kwargs: dict) -> tuple[bool, str]:
        """真正执行一次讨饭，Tool 和 Command 共用。"""
        root = self.config
        if not root.plugin.enabled or not root.beg.enabled:
            return False, "讨饭功能已关闭。"

        stream_id = self._resolve_stream(kwargs)
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

        # 先占位再发送：从检查到写状态之间有一段 await，两个并发调用会同时通过
        # 上面的冷却/上限检查，结果两张收款码都发出去、计数却只加一次。
        # 占位放在第一个 await 之前就没有这个窗口；发送失败再回滚。
        prev_last = self._last_sent.get(stream_id)
        prev_daily = self._daily.get(stream_id)
        self._last_sent[stream_id] = now
        self._daily[stream_id] = (day, count + 1)

        sender = str(root.beg.sender_name or "").strip() or "麦麦"
        delivered = False
        try:
            try:
                b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            except Exception as exc:
                self.ctx.logger.error("读取收款码失败: %s", exc)
                return False, "收款码读不出来，发不了。"

            # 合并转发里**只放图片**：讨饭的话由模型在正常回复里说，
            # 转发里再重复会显得啰嗦。
            #
            # ⚠ send.forward / send.image 失败时**不抛异常**，而是返回 False
            # （SDK 把它们列进 _BOOLEAN_SUCCESS_CAPABILITIES）。只看有没有异常会把
            # 「没发出去」当成「发出去了」，既不重试也不报错，用户那边什么都没有。
            try:
                delivered = bool(
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
                )
            except Exception as exc:
                self.ctx.logger.warning("合并转发失败，退回直接发图: %s", exc)
                delivered = False

            if not delivered:
                self.ctx.logger.info("合并转发没成功，改用直接发图")
                try:
                    delivered = bool(await self.ctx.send.image(b64, stream_id))
                except Exception as exc:
                    self.ctx.logger.error("发送收款码失败: %s", exc)
                    delivered = False

            if not delivered:
                return False, "收款码没发出去。"
        finally:
            if not delivered:
                # 发送失败：把占位退回去，别让一次失败白占冷却和每日额度
                if prev_last is None:
                    self._last_sent.pop(stream_id, None)
                else:
                    self._last_sent[stream_id] = prev_last
                if prev_daily is None:
                    self._daily.pop(stream_id, None)
                else:
                    self._daily[stream_id] = prev_daily

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
            # ⚠ 账本是纯手工维护的唯一副本，解析失败绝不能当成「空账本」继续跑：
            # 下一次记账会用「空列表 + 这一笔」把整个文件 replace 掉，历史全没。
            # 这里把坏文件改名留档，并置位禁止写入，等用户自己处理。
            self.ctx.logger.error("账本读取失败: %s", exc)
            self._ledger = []
            self._ledger_broken = True
            try:
                backup = self._ledger_path.with_name(
                    f"{self._ledger_path.name}.corrupt-{int(time.time())}"
                )
                self._ledger_path.replace(backup)
                self.ctx.logger.error(
                    "坏账本已备份为 %s，本次运行不写入账本，请手工修好后再重启",
                    backup.name,
                )
            except Exception as exc2:
                self.ctx.logger.error("坏账本备份失败，为避免覆盖已停止写入: %s", exc2)

    def _write_json(self, path: Path | None, payload: Any, label: str) -> bool:
        """原子写：先写同目录临时文件，再 os.replace 换上去。返回是否写成功。

        账本是纯手工维护的唯一副本，直接覆盖时如果写到一半进程没了，
        留下的就是半个 JSON，那份数据真的救不回来。
        """
        if path is None:
            return False
        # 临时文件名要唯一：热重载时新旧实例可能短暂并存，共用固定名会互相踩
        # （A 写 tmp → B 截断重写 → A 把 B 的半成品换上正式文件）。
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
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
        if self._ledger_broken:
            self.ctx.logger.error("账本文件损坏已禁用写入，本次记账不会落盘")
            return False
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
        # NaN 和 inf 能穿过下面两道比较（nan <= 0 和 nan > 10000 都是 False），
        # 一旦写进账本，之后所有 sum 都变 nan，json 里还会出现非标准的 NaN 字面量。
        if not math.isfinite(value):
            return False, "金额不是有效数字，记不了。"
        if value <= 0:
            return False, "金额要大于 0。"
        if value > 10000:
            return False, "金额太大了，先确认一下再记。"

        unit = root.ledger.currency

        # 群友触发 → 先问管理员；管理员自己触发就直接记。
        if root.ledger.require_approval and not is_admin:
            if not self._admin_ids():
                return False, "还没配置管理员 QQ 号，记账审批用不了。"
            # 同一条流 30 秒内只报一次：不然连发 /打赏 1 能把管理员私聊刷爆
            now = time.monotonic()
            last = self._last_approval.get(stream_id)
            if last is not None and now - last < 30:
                return False, "刚报过一笔，等管理员处理完再说。"
            self._last_approval[stream_id] = now
            rec = await self._create_approval(value, note, stream_id, kwargs or {})
            if rec is None:
                # 没建立成功，把节流标记退回去，别让一次失败也占 30 秒
                self._last_approval.pop(stream_id, None)
                return False, "联系不上管理员（管理员可能还没私聊过机器人），这笔先记不了。"
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
                # 同样要接返回值：send.text 发失败只返回 False，不抛异常。
                # 不判的话会把「没送到」当成「送到了」，审批记录照样落盘，
                # 管理员手里却什么都没有，那个编号永远等不到人来确认。
                ok = bool(await self.ctx.send.text(text, sid))
            except Exception as exc:
                self.ctx.logger.warning("私聊管理员 %s 失败: %s", uid, exc)
                continue
            if not ok:
                self.ctx.logger.warning("私聊管理员 %s 没发出去，宿主返回失败", uid)
                continue
            delivered += 1
        return delivered > 0

    async def _create_approval(
        self, amount: float, note: str, stream_id: str, kwargs: dict
    ) -> dict[str, Any] | None:
        """生成待确认记录并私聊管理员。送不到就返回 None，不落账。"""
        # 顺手清掉过期的：只有 on_load / /待确认 / 确认时才会 purge，
        # 长期没人处理的话 _approvals 会一直长。
        self._purge_approvals()

        _, nickname, group_name = self._caller_info(kwargs)
        self._approval_seq += 1
        rec: dict[str, Any] = {
            "id": str(self._approval_seq),
            "amount": round(float(amount), 2),
            "note": re.sub(r"[\r\n\t]+", " ", str(note or "")).strip()[:40],
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

        # 先登记落盘再私聊：`_dm_admins` 要串行 await 每个管理员，
        # 管理员收到编号后立刻回 /确认 的话，记录还没进内存就会「找不到编号」。
        self._approvals[rec["id"]] = rec
        if not self._save_approvals():
            self._approvals.pop(rec["id"], None)
            self.ctx.logger.error("待确认记录落盘失败，编号 %s 作废", rec["id"])
            return None

        if not await self._dm_admins("\n".join(lines)):
            # 一个管理员都没送到：把刚登记的记录撤掉，别留个没人知道的编号
            self._approvals.pop(rec["id"], None)
            self._save_approvals()
            return None

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
            if not isinstance(raw, dict) or not str(key).isdigit():
                dropped += 1
                continue
            try:
                clean[str(key)] = {
                    # 以字典键为准：文件被手工改过时可能出现 key="5"、id="3"，
                    # 那样 /待确认 显示 #3、管理员回 /确认 3 找不到，
                    # 而不带编号确认时 pop("3") 又删不掉键 "5" → 下次重复记账。
                    "id": str(key),
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

    def _save_approvals(self) -> bool:
        return self._write_json(
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
        # 补零：`1-5` 也要能认，不然只认 `01-05` 这种写法
        key = raw
        if re.fullmatch(r"\d{1,2}-\d{1,2}", raw):
            month, day = raw.split("-")
            key = f"{date.today().year}-{int(month):02d}-{int(day):02d}"
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
