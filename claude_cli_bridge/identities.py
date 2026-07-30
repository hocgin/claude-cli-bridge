"""Identity prompts and per-identity defaults.

Kept compatible with workbuddy_bridge/identities.py (same four roles, same
behavioural boundaries) but extended with the per-identity knobs the Claude CLI
exposes: a default model, reasoning effort, and the subset of tools each role
is allowed to use.

The identity prompt is injected verbatim via ``--append-system-prompt``; callers
must never repeat the identity instructions inside their prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

REVIEW_IDENTITIES = frozenset({"S1", "S2", "S3"})

# Roles may differ in cost/speed.  GLM-5.2 is cheap and fast, good enough for
# search and the syntactic S1 sweep; S2/S3 lean on stronger reasoning.
DEFAULT_MODEL = "glm-5.2"


@dataclass(frozen=True)
class Identity:
    """One named worker role."""

    name: str
    prompt: str
    model: str = DEFAULT_MODEL
    effort: str = "low"
    # Empty tuple == inherit the CLI default tool set.  review identities keep
    # file read access but may not edit or run shell commands.
    allowed_tools: tuple[str, ...] = ()


IDENTITIES: dict[str, Identity] = {
    "online-search": Identity(
        name="online-search",
        model="glm-5.2",
        effort="low",
        prompt="""你是 online-search，专门负责联网检索、资料核对和来源整理。

执行要求：
- 对时效信息明确核对发布日期和事件发生日期。
- 优先使用第一方或权威来源，并保留可访问的原始链接。
- 区分已证实事实、来源主张和你的推断。
- 结论简洁明确；资料不足时明确指出缺口，不要猜测。""",
    ),
    "S1": Identity(
        name="S1",
        model="glm-5.2",
        effort="low",
        allowed_tools=("Read", "Grep", "Glob"),
        prompt="""你是 S1，负责代码审核中的语法检查。
只做纯文本审核，不要打开浏览器，不要截图，不要做任何界面模拟或视觉操作。

重点检查：
- 语法错误
- 拼写错误
- 明显的代码层面错误
- 易于直接发现的低级问题

输出要求：
- 结论简洁明确
- 先指出问题，再说明原因
- 不要修改文件，除非用户明确要求""",
    ),
    "S2": Identity(
        name="S2",
        model="glm-5.2",
        effort="medium",
        allowed_tools=("Read", "Grep", "Glob", "Bash"),
        prompt="""你是 S2，负责代码审核中的依赖安全扫描。
只做纯文本审核，不要打开浏览器，不要截图，不要做任何界面模拟或视觉操作。

重点检查：
- 依赖漏洞
- 过时或高风险依赖
- 已知安全隐患
- 供应链风险线索

输出要求：
- 结论简洁明确
- 先给出风险等级或是否有风险
- 再说明具体依赖和原因
- 不要修改文件，除非用户明确要求""",
    ),
    "S3": Identity(
        name="S3",
        # S3 benefits from deeper reasoning about maintainability.
        model="sonnet",
        effort="high",
        allowed_tools=("Read", "Grep", "Glob"),
        prompt="""你是 S3，负责代码审核中的代码规范检查。
只做纯文本审核，不要打开浏览器，不要截图，不要做任何界面模拟或视觉操作。

重点检查：
- 命名是否清晰一致
- 格式和风格是否统一
- 结构是否符合常见规范
- 是否存在可维护性问题

输出要求：
- 结论简洁明确
- 先指出规范问题，再给出建议
- 不要修改文件，除非用户明确要求""",
    ),
}


_IDENTITY_ALIASES = {
    "online-search": "online-search",
    "online_search": "online-search",
    "search": "online-search",
    "s1": "S1",
    "s2": "S2",
    "s3": "S3",
}


def normalize_identity(identity: str) -> str:
    """Return the canonical identity name, or '' for an empty/blank input."""
    value = (identity or "").strip()
    if not value:
        return ""
    canonical = _IDENTITY_ALIASES.get(value.lower())
    if not canonical:
        choices = ", ".join(IDENTITIES)
        raise ValueError(f"identity must be one of: {choices}")
    return canonical


def get_identity(identity: str) -> Identity | None:
    canonical = normalize_identity(identity)
    return IDENTITIES.get(canonical) if canonical else None


@dataclass
class IdentityOverrides:
    """Effective model / effort / tools / system-prompt after identity merge."""

    model: str = ""
    effort: str = ""
    allowed_tools: tuple[str, ...] = ()
    identity_prompt: str = ""
    canonical: str = ""

    @classmethod
    def resolve(
        cls,
        identity: str,
        *,
        model: str = "",
        effort: str = "",
        allowed_tools: tuple[str, ...] = (),
    ) -> "IdentityOverrides":
        """Merge caller overrides on top of the identity's defaults.

        Explicit caller values always win; otherwise the identity default
        applies; plain-text tasks with no identity fall back to "".
        """
        canonical = normalize_identity(identity)
        ident = IDENTITIES.get(canonical) if canonical else None
        if ident is None:
            return cls(
                model=model,
                effort=effort,
                allowed_tools=allowed_tools,
                canonical="",
            )
        return cls(
            canonical=canonical,
            model=(model.strip() or ident.model),
            effort=(effort.strip() or ident.effort),
            # Caller-provided tools fully replace the identity's set, matching
            # the CLI's --allowed-tools semantics (not additive).
            allowed_tools=(allowed_tools or ident.allowed_tools),
            identity_prompt=ident.prompt,
        )


def compose_identity_prompt(identity: str, task: str) -> str:
    """Compose the identity instruction + task body (legacy helper).

    server.py injects the prompt separately via --append-system-prompt, so the
    CLI receives the task body alone; this helper is kept for parity with the
    reference project's test entrypoints.
    """
    overrides = IdentityOverrides.resolve(identity)
    if not overrides.identity_prompt:
        return task
    return f"{overrides.identity_prompt}\n\n任务：\n{task}"
