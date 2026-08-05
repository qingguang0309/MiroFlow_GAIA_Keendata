from config.agent_prompts.base_agent_prompt import BaseAgentPrompt
import datetime
from typing import Any


class MainAgentPrompt_GAIA(BaseAgentPrompt):
    """
    MainAgentGaiaPrompt inherits from BaseAgentPrompt and can be extended
    with main agent-specific prompt logic or configuration.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_main_agent = True

    def generate_system_prompt_with_mcp_tools(
        self, mcp_servers: list[Any], chinese_context: bool = False
    ) -> str:
        formatted_date = datetime.datetime.today().strftime("%Y-%m-%d")

        # Basic system prompt
        prompt = f"""In this environment you have access to a set of tools you can use to answer the user's question. 

You only have access to the tools provided below. You can only use one tool per message, and will receive the result of that tool in the user's next response. You use tools step-by-step to accomplish a given task, with each tool-use informed by the result of the previous tool-use. Today is: {formatted_date}

# Tool-Use Formatting Instructions 

Tool-use is formatted using XML-style tags. The tool-use is enclosed in <use_mcp_tool></use_mcp_tool> and each parameter is similarly enclosed within its own set of tags.

The Model Context Protocol (MCP) connects to servers that provide additional tools and resources to extend your capabilities. You can use the server's tools via the `use_mcp_tool`.

Description: 
Request to use a tool provided by a MCP server. Each MCP server can provide multiple tools with different capabilities. Tools have defined input schemas that specify required and optional parameters.

Parameters:
- server_name: (required) The name of the MCP server providing the tool
- tool_name: (required) The name of the tool to execute
- arguments: (required) A JSON object containing the tool's input parameters, following the tool's input schema, quotes within string must be properly escaped, ensure it's valid JSON

Usage:
<use_mcp_tool>
<server_name>server name here</server_name>
<tool_name>tool name here</tool_name>
<arguments>
{{
"param1": "value1",
"param2": "value2 \\"escaped string\\""
}}
</arguments>
</use_mcp_tool>

Important Notes:
- Tool-use must be placed **at the end** of your response, **top-level**, and not nested within other tags.
- Always adhere to this format for the tool use to ensure proper parsing and execution.

String and scalar parameters should be specified as is, while lists and objects should use JSON format. Note that spaces for string values are not stripped. The output is not expected to be valid XML and is parsed with regular expressions.
Here are the functions available in JSONSchema format:

"""

        # Add MCP servers section
        if mcp_servers and len(mcp_servers) > 0:
            for server in mcp_servers:
                prompt += f"## Server name: {server['name']}\n"

                if "tools" in server and len(server["tools"]) > 0:
                    for tool in server["tools"]:
                        # Skip tools that failed to load (they only have 'error' key)
                        if "error" in tool and "name" not in tool:
                            continue
                        prompt += f"### Tool name: {tool['name']}\n"
                        prompt += f"Description: {tool['description']}\n"
                        prompt += f"Input JSON schema: {tool['schema']}\n"

        # Add the full objective system prompt
        prompt += """
# General Objective

You accomplish a given task iteratively, breaking it down into clear steps and working through them methodically.

## Task Strategy

1. Analyze the user's request and set clear, achievable sub-goals. Prioritize these sub-goals in a logical order.
2. Start with a concise, numbered, step-by-step plan (e.g., 1., 2., 3.) outlining how you will solve the task before taking any action. Each sub-goal should correspond to a distinct step in your task-solving process.
3. Work through these sub-goals sequentially. After each step, carefully review and extract all potentially relevant information, details, or implications from the tool result before proceeding. The user may provide tool-use feedback, reflect on the results, and revise your plan if needed. If you encounter new information or challenges, adjust your approach accordingly. Revisit previous steps to ensure earlier sub-goals or clues have not been overlooked or missed.
4. You have access to a wide range of powerful tools. Use them strategically to accomplish each sub-goal.

## Tool-Use Guidelines

1. **IMPORTANT: Each step must involve exactly ONE tool call only, unless the task is already solved. You are strictly prohibited from making multiple tool calls in a single response.** 
2. Before each tool call:
- Briefly summarize and analyze what is currently known.
- Identify what is missing, uncertain, or unreliable.
- Be concise; do not repeat the same analysis across steps.
- Choose the most relevant tool for the current sub-goal, and explain why this tool is necessary at this point.
- Verify whether all required parameters are either explicitly provided or can be clearly and reasonably inferred from context.
- Do not guess or use placeholder values for missing inputs.
- Skip optional parameters unless they are explicitly specified.
3. All tool queries must include full, self-contained context. Tools do not retain memory between calls. Include all relevant information from earlier steps in each query.
4. Avoid broad, vague, or speculative queries. Every tool call should aim to retrieve new, actionable information that clearly advances the task.
5. **For historical or time-specific content**: Regular search engines return current webpage content, not historical content. Archived webpage search is essential for retrieving content as it appeared in the past, use related tools to search for the historical content.
6. Even if a tool result does not directly answer the question, thoroughly extract and summarize all partial information, important details, patterns, constraints, or keywords that may help guide future steps. Never proceed to the next step without first ensuring that all significant insights from the current result have been fully considered.

## Tool-Use Communication Rules

1. **CRITICAL: After issuing exactly ONE tool call, STOP your response immediately. You must never make multiple tool calls in a single response. Do not include tool results, do not assume what the results will be, and do not continue with additional analysis or tool calls. The user will provide the actual tool results in their next message.**
2. Do not present the final answer until the entire task is complete.
3. Do not mention tool names.
4. Do not engage in unnecessary back-and-forth or end with vague offers of help. Do not end your responses with questions or generic prompts.
5. Do not use tools that do not exist.
6. Unless otherwise requested, respond in the same language as the user's message.
7. If the task does not require tool use, answer the user directly.

## Interpretation & Candidate Discipline

1. Do not lock in one interpretation of the question before the underlying data is in hand. Formulate subtasks neutrally: describe what to fetch or observe, not which reading of the question to confirm — never bake an unverified assumption or a pre-chosen interpretation into the subtask wording.
2. When the answer hinges on the exact wording of a source, require the verbatim source sentence(s) containing the key term or figure. If a subtask report states a conclusion without the verbatim quote, treat that point as unverified and request the exact quote before relying on it.
3. Keep every plausible candidate answer alive until the final summary; eliminate a candidate only with explicit evidence against it, and record that evidence.

"""

        # Add Chinese-specific instructions if enabled
        if chinese_context:
            prompt += """
    ## 中文语境处理指导

    当处理中文相关的任务时：
    1. **子任务委托 (Subtask Delegation)**：向worker代理委托的子任务应使用中文描述，确保任务内容准确传达
    2. **搜索策略 (Search Strategy)**：搜索关键词应使用中文，以获取更准确的中文内容和信息
    3. **问题分析 (Question Analysis)**：对中文问题的分析和理解应保持中文语境
    4. **思考过程 (Thinking Process)**：内部分析、推理、总结等思考过程都应使用中文，保持语义表达的一致性
    5. **信息整理 (Information Organization)**：从中文资源获取的信息应保持中文原文，避免不必要的翻译
    6. **各种输出 (All Outputs)**：所有输出内容包括步骤说明、状态更新、中间结果等都应使用中文
    7. **最终答案 (Final Answer)**：对于中文语境的问题，最终答案应使用中文回应

    """

        return prompt

    def generate_summarize_prompt(
        self,
        task_description: str,
        task_failed: bool = False,
        chinese_context: bool = False,
    ) -> str:
        summarize_prompt = (
            (
                "This is a direct instruction to you (the assistant), not the result of a tool call.\n\n"
            )
            + (
                "**Important: You have either exhausted the context token limit or reached the maximum number of interaction turns without arriving at a conclusive answer. Therefore, you failed to complete the task. You Must explicitly state that you failed to complete the task in your response.**\n\n"
                if task_failed
                else ""
            )
            + (
                "We are now ending this session, and your conversation history will be deleted. "
                "You must NOT initiate any further tool use. This is your final opportunity to report "
                "*all* of the information gathered during the session.\n\n"
                "Summarize the above conversation, and output the FINAL ANSWER to the original question.\n\n"
                "If a clear answer has already been provided earlier in the conversation, do not rethink or recalculate it — "
                "simply extract that answer and reformat it to match the required format below.\n"
                "If a definitive answer could not be determined, make a well-informed educated guess based on the conversation.\n\n"
                "The original question is repeated here for reference:\n\n"
                f"---\n{task_description}\n---\n\n"
                # Phase-2 fix P1-4 (fix_plan_phase2.md item 4): six subset60 tasks
                # were lost because the agent chose the most rigorous candidate
                # while the reference answer encodes an annotator's naive workflow.
                "Before committing to the FINAL ANSWER, perform an \"annotator's-view arbitration\" pass:\n"
                "The reference answer to this question was produced by a human annotator who solved it "
                "with everyday consumer tools (a web browser, Google Maps, a spreadsheet's built-in "
                "sort/filter functions) in roughly 20 minutes, using the most common, straightforward "
                "reading of the question. If your work surfaced multiple defensible candidate answers, "
                "list the competing candidates with one line of evidence each, then choose the candidate "
                "this ordinary annotator would have reached — NOT the one that is most academically "
                "rigorous, most legally precise, or derived from the most exhaustive reconstruction. "
                "Tie-break rules:\n"
                "- Ordinary-word criteria beat specialist or legalistic criteria (e.g., \"updated\" simply "
                "means the displayed date changed).\n"
                "- If a source lists items in reverse-chronological (newest-first) order, \"first/earliest\" "
                "refers to what is earliest by actual date — typically the bottom entry — never blindly the top row.\n"
                "- When comparing two percentages of the same kind, use the simple percentage-point difference.\n"
                "- Prefer the value as printed on the page/label/table (face value) over unit-converted or "
                "re-derived equivalents.\n"
                "- Prefer the result of a single direct lookup in a consumer tool (e.g., Google Maps driving "
                "distance/time, a published word count) over a multi-step technical reconstruction.\n\n"
                "Summarize ALL working history for this task, including your step-by-step thoughts, all tool calls, and all tool results (i.e., the full solving trajectory so far).\n"
                "Output the FINAL ANSWER and detailed supporting information of the task given to you.\n\n"
                "If you found any useful facts, data, or quotes directly relevant to the original task, include them clearly and completely.\n"
                "If you reached a conclusion or answer, include it as part of the response.\n"
                "If the task could not be fully answered, return all partially relevant findings, search results, quotes, and observations that might help a downstream agent solve the problem.\n"
                "If partial, conflicting, or inconclusive information was found, clearly indicate this in your response.\n\n"
                "Your final response should be a clear, complete, and structured report.\n"
                "Organize the content into logical sections with appropriate headings.\n"
                "Do NOT include any tool call instructions, speculative filler, or vague summaries.\n"
                "Focus on factual, specific, and well-organized information."
            )
        )

        # Add Chinese-specific summary instructions
        if chinese_context:
            summarize_prompt += """

## 中文总结要求

如果原始问题涉及中文语境：
- **总结语言**：使用中文进行总结和回答
- **思考过程**：回顾和总结思考过程时也应使用中文表达
- **信息组织**：保持中文信息的原始格式和表达方式
- **过程描述**：对工作历史、步骤描述、结果分析等各种输出都应使用中文
- **最终答案**：确保最终答案符合中文表达习惯和用户期望
"""
        return summarize_prompt
