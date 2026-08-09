# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

import pathlib
import sys
import importlib
from mcp import StdioServerParameters
from omegaconf import DictConfig, OmegaConf

from src.logging.logger import bootstrap_logger
from config.agent_prompts.base_agent_prompt import BaseAgentPrompt

import os

LOGGER_LEVEL = os.getenv("LOGGER_LEVEL", "INFO")
logger = bootstrap_logger(level=LOGGER_LEVEL)

# Proxy env vars to propagate into MCP server subprocesses. The MCP SDK starts
# stdio servers with only a small safe-env allowlist (HOME/PATH/USER/...), and
# each tool yaml declares only its API keys — so HTTP(S)_PROXY/NO_PROXY set in
# the parent (e.g. loaded from .env) would NOT reach the subprocess. Tools that
# fetch blocked domains (archived pages, Gemini, github raw) issue those
# requests from inside their own process via requests/httpx, which read these
# vars. On macOS the parent has none of these set (system proxy is used
# instead), so this injection is a no-op there and local behavior is unchanged;
# on a headless Linux box behind an ssh tunnel, it is what makes the tunnel
# actually apply.
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def _with_proxy_env(env: dict) -> dict:
    merged = dict(env)
    for key in _PROXY_ENV_KEYS:
        if key in os.environ and key not in merged:
            merged[key] = os.environ[key]
    return merged


# MCP server configuration generation function
def create_mcp_server_parameters(
    cfg: DictConfig, agent_cfg: DictConfig, logs_dir: str | None = None
):
    """Define and return MCP server configuration list"""
    configs = []

    if agent_cfg.get("tool_config", None) is not None:
        for tool in agent_cfg["tool_config"]:
            try:
                config_path = (
                    pathlib.Path(__file__).parent.parent.parent
                    / "config"
                    / "tool"
                    / f"{tool}.yaml"
                )
                tool_cfg = OmegaConf.load(config_path)
                configs.append(
                    {
                        "name": tool_cfg.get("name", tool),
                        "params": StdioServerParameters(
                            command=sys.executable
                            if tool_cfg["tool_command"] == "python"
                            else tool_cfg["tool_command"],
                            args=tool_cfg.get("args", []),
                            env=_with_proxy_env(tool_cfg.get("env", {})),
                        ),
                    }
                )
            except Exception as e:
                logger.error(
                    f"[ERROR] Error creating MCP server parameters for tool {tool}: {e}"
                )
                continue

    blacklist = set()
    for black_list_item in agent_cfg.get("tool_blacklist", []):
        blacklist.add((black_list_item[0], black_list_item[1]))
    return configs, blacklist


def _load_agent_prompt_class(prompt_class_name: str) -> BaseAgentPrompt:
    # Dynamically import the class from the config.agent_prompts module
    if not isinstance(prompt_class_name, str) or not prompt_class_name.isidentifier():
        raise ValueError(f"Invalid prompt class name: {prompt_class_name}")

    try:
        # Import the module dynamically
        agent_prompts_module = importlib.import_module("config.agent_prompts")
        # Get the class from the module
        PromptClass = getattr(agent_prompts_module, prompt_class_name)
    except (ModuleNotFoundError, AttributeError) as e:
        raise ImportError(
            f"Could not import class '{prompt_class_name}' from 'config.agent_prompts': {e}"
        )
    return PromptClass()


def expose_sub_agents_as_tools(sub_agents_cfg: DictConfig):
    """Expose sub-agents as tools"""
    sub_agents_server_params = []
    for sub_agent in sub_agents_cfg.keys():
        if not sub_agent.startswith("agent-"):
            raise ValueError(
                f"Sub-agent name must start with 'agent-': {sub_agent}. Please check the sub-agent name in the agent's config file."
            )
        try:
            sub_agent_prompt_instance = _load_agent_prompt_class(
                sub_agents_cfg[sub_agent].prompt_class
            )
            sub_agent_tool_definition = sub_agent_prompt_instance.expose_agent_as_tool(
                subagent_name=sub_agent
            )
            sub_agents_server_params.append(sub_agent_tool_definition)
        except Exception as e:
            raise ValueError(f"Failed to expose sub-agent {sub_agent} as a tool: {e}")
    return sub_agents_server_params
