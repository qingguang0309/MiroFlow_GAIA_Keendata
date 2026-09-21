"""API / tool error signatures shared by preflight_api.py and scan_trace_errors.py.

Background (E35, 2026-09-13): a launch script overrode KIMI_API_KEY with an OpenRouter
key but left KIMI_BASE_URL on Moonshot. tool-reasoning and tool-image-video then got
401 on every call for a whole run. The errors only showed up inside tool results in the
agent conversation, never in run.out, so nothing stopped the run.

Only failures of OUR API calls count (LLM providers and tool backends: OpenRouter,
Moonshot, Gemini, OpenAI audio, Serper, Jina, E2B). Errors from websites the agent
visits or from code the agent runs in the sandbox are part of normal task solving and
are never blocking.

Categories:
  persistent (a key, account, model or tool backend is broken; never fixes itself):
             quota, auth, model, tool_infra, filtered
  transient  (may clear on retry): rate, server, conn, timeout, request400
  tool_error (content-level tool outcome, not blocking)

"Server 'X' not found" is NOT tool_infra: it is the agent naming a server that does not exist
(e.g. tool-python-code); a content-level mistake, not an API/tool failure.
tool_infra was added after the f3 scan found tool-reading unable to start its
markitdown backend on AWS ("No such file or directory: 'uv'") in 31 of 165 tasks,
silently, for the whole run.
"""
import re

PERSISTENT = {"quota", "auth", "model", "tool_infra", "filtered"}
TRANSIENT = {"rate", "server", "conn", "timeout", "request400"}

# Evidence that a text is an error report rather than page content containing words like "Unauthorized".
ERROR_CONTEXT = re.compile(
    r"\[ERROR\]|Error code: \d{3}|failed after \d+ (retries|attempts)|after \d+ attempts|Failed to (create|connect)|"
    r"Serper API error|Jina|OpenAI Error|Gemini Error|Claude Error|[A-Za-z]+Error\b|'error':|\"error\":|content_filter",
    re.I,
)

# Failures of something that is not our provider: a scraped site's own status, or HTTP
# errors raised by code the agent ran.
# Our own provider hosts. A status reported "for url: <one of these>" is ours; anything else is the
# fetched site's own status. r.jina.ai is deliberately absent: its URL embeds the target
# (r.jina.ai/https://site/...), so its status reflects the target, not our Jina account. Real Jina
# account failures say so in words ("Jina ... api key/quota/balance") and are matched by SIGNATURES.
OUR_HOSTS = (
    r"openrouter\.ai|api\.moonshot\.cn|generativelanguage\.googleapis\.com|api\.openai\.com|"
    r"google\.serper\.dev|[\w.-]*\.e2b\.(?:dev|app)|api\.anthropic\.com"
)

# Failures of something that is not our provider: a scraped site's own status, or HTTP
# errors raised by code the agent ran.
# E39 (2026-09-20): the requests raise_for_status() wording was missing, so a paywalled news site
# answering "402 Client Error: Payment Required for url: https://people.com/..." was classified as
# our own quota being exhausted and froze a run on its first occurrence.
THIRD_PARTY = re.compile(
    r"Target URL returned error|HTTPSConnectionPool\(host=|HTTP Error \d{3}:|urllib3?\.exceptions|requests\.exceptions|"
    r"\d{3} (?:Client|Server) Error:[^\n]{0,120}? for url: (?!https?://(?:[\w.-]*\.)?(?:" + OUR_HOSTS + r"))",
    re.I,
)

SIGNATURES = [
    ("quota", re.compile(
        r"suspended due to insufficient balance|exceeded_current_quota|insufficient[_ ](balance|credits|quota)|"
        r"Error code: 402|Payment Required", re.I)),
    ("auth", re.compile(
        r"Error code: 40[13]\b|invalid_authentication|Invalid Authentication|Incorrect API key|invalid[_ ]api[_ ]key|"
        r"API_KEY_DISABLED|API key is disabled|AuthenticationError|PermissionDenied|violation of provider Terms", re.I)),
    ("model", re.compile(
        r"is no longer available|model[_ ]not[_ ]found|does not exist or you do not have access|404 NOT_FOUND|"
        r"No endpoints found for", re.I)),
    ("tool_infra", re.compile(
        r"Failed to connect to [\w.-]+ server|No such file or directory: '(uv|uvx|npx|node)'|"
        r"Cannot connect or get tools from server", re.I)),
    ("filtered", re.compile(
        r"finish_reason=content_filter|unsupported finish reason: content_filter|"
        r"blocked by the provider content filter", re.I)),
    ("rate", re.compile(r"Error code: 429|RateLimitError|Too Many Requests|Rate limit (exceeded|reached)|\b429: |RESOURCE_EXHAUSTED", re.I)),
    ("server", re.compile(
        r"Error code: 5\d\d|InternalServerError|Internal Server Error|Service (temporarily )?Unavailable|Bad Gateway|"
        r"Gateway Time-?out|Serper API error: 5\d\d", re.I)),
    ("conn", re.compile(r"APIConnectionError|Connection error|ConnectTimeout|ReadTimeout|Max retries exceeded", re.I)),
    ("timeout", re.compile(r"Tool execution timeout", re.I)),
    ("request400", re.compile(r"Error code: 400|BadRequestError|Invalid request|unsupported image", re.I)),
]
TOOL_ERROR = re.compile(r"\[ERROR\]|failed after \d+ (retries|attempts)|Traceback \(most recent call last\)|Tool execution timeout", re.I)

# tool-code returns the agent's own program output; only the sandbox service itself counts.
SANDBOX_SERVICE = re.compile(r"Failed to create sandbox|sandbox[^\n]{0,80}(40[13]|429|5\d\d|Unauthorized|Invalid API key|quota)", re.I)


def classify(text, head_chars=1500, server=None):
    """Return (category, blocking) for one tool result, or (None, False).

    server: MCP server name the result came from, when known. Results of the sub-agent
    tool (agent-worker) are reports that quote other errors and are never blocking.
    """
    head = (text or "")[:head_chars]
    generic = bool(TOOL_ERROR.search(head))
    if server == "agent-worker":
        return ("tool_error", False) if generic else (None, False)
    if server == "tool-code" and not SANDBOX_SERVICE.search(head):
        return ("tool_error", False) if generic else (None, False)
    if ERROR_CONTEXT.search(head) and not THIRD_PARTY.search(head):
        for name, rx in SIGNATURES:
            if rx.search(head):
                return name, True
    return ("tool_error", False) if generic else (None, False)
