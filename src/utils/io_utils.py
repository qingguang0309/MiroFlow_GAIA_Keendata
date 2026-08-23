# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

import os
import re

from src.logging.logger import bootstrap_logger


LOGGER_LEVEL = os.getenv("LOGGER_LEVEL", "INFO")
logger = bootstrap_logger(level=LOGGER_LEVEL)


def process_input(task_description, task_file_name):
    """
    Process user input, especially files.
    Returns formatted initial user message content list and updated task description.
    """
    initial_user_content = []
    updated_task_description = task_description

    # todo: add the key of `url` here for differentiating youtube wikipedia and normal url

    if task_file_name:
        if not os.path.isfile(task_file_name):
            raise FileNotFoundError(f"Error: File not found {task_file_name}")
        file_extension = task_file_name.rsplit(".", maxsplit=1)[-1].lower()
        file_type = None
        if file_extension in ["jpg", "jpeg", "png", "gif", "webp"]:
            file_type = "Image"
        elif file_extension == "txt":
            file_type = "Text"
        elif file_extension in ["jsonld", "json"]:
            file_type = "Json"
        elif file_extension in ["xlsx", "xls"]:
            file_type = "Excel"
        elif file_extension == "pdf":
            file_type = "PDF"
        elif file_extension in ["docx", "doc"]:
            file_type = "Document"
        elif file_extension in ["html", "htm"]:
            file_type = "HTML"
        elif file_extension in ["pptx", "ppt"]:
            file_type = "PPT"
        elif file_extension in ["wav"]:
            file_type = "WAV"
        elif file_extension in ["mp3", "m4a"]:
            file_type = "MP3"
        elif file_extension in ["zip"]:
            file_type = "Zip"
        else:
            file_type = file_extension
        # Get the absolute path of the file
        absolute_file_path = os.path.abspath(task_file_name)
        updated_task_description += f"\nNote: A {file_type} file '{task_file_name}' is associated with this task. If you need worker agent to read its content, you should provide the complete local system file path: {absolute_file_path}.\n\n"

        logger.info(
            f"Info: Detected {file_type} file {task_file_name}, added hint to description."
        )
    # output format requiremnt
    # updated_task_description += "\nYou should follow the format instruction in the question strictly and wrap the final answer in \\boxed{}."

    # Add text content (may have been updated)
    initial_user_content.append({"type": "text", "text": updated_task_description})

    return initial_user_content, updated_task_description


class OutputFormatter:
    def _extract_boxed_content(self, text: str) -> str:
        """
        Extract content from \\boxed{} patterns in the text.
        Uses balanced brace counting to handle arbitrary levels of nested braces correctly.
        Returns the last matched content, or empty string if no match found.
        """
        if not text:
            return ""

        matches = []
        i = 0

        while i < len(text):
            # Find the next \boxed{ pattern
            boxed_start = text.find(r"\boxed{", i)
            if boxed_start == -1:
                break

            # Start after the opening brace
            content_start = boxed_start + 7  # len(r'\boxed{') = 7
            if content_start >= len(text):
                break

            # Count balanced braces
            brace_count = 1
            content_end = content_start

            while content_end < len(text) and brace_count > 0:
                char = text[content_end]
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                content_end += 1

            # If we found a balanced match (brace_count == 0)
            if brace_count == 0:
                content = text[
                    content_start : content_end - 1
                ]  # -1 to exclude the closing brace
                matches.append(content)
                # Continue searching from after this complete match
                i = content_end
            else:
                # If braces are unbalanced, skip this \boxed{ and continue searching
                i = content_start

        if not matches:
            return ""
        content = self._strip_latex_text_wrapper(matches[-1])
        return self._clean_latex_artifacts(content)

    @staticmethod
    def _clean_latex_artifacts(content: str) -> str:
        """
        Remove cosmetic LaTeX markup left INSIDE the boxed answer (beyond the
        whole-content wrappers handled by _strip_latex_text_wrapper), e.g.
        "101.376\\ \\text{CFM},\\ 84.348\\ \\text{CFM}". GAIA reference answers
        never contain LaTeX markup, so these artifacts can only hurt
        exact-match scoring. Math structure commands (\\frac, \\sqrt, ...) are
        left untouched.
        """
        if "\\" not in content:
            return content
        # Interior text-style commands: \text{X} -> X. Innermost-first via the
        # brace-free payload restriction, repeated until stable.
        text_cmd = re.compile(
            r"\\(?:text|textbf|textit|textrm|texttt|mathrm|mathit)\{([^{}]*)\}"
        )
        prev = None
        while prev != content:
            prev = content
            content = text_cmd.sub(r"\1", content)
        # LaTeX spacing macros -> regular space.
        content = re.sub(r"\\[ ,;:!]|\\qquad|\\quad", " ", content)
        # Escaped literal characters -> the characters themselves.
        content = re.sub(r"\\([%$&#_])", r"\1", content)
        # Collapse whitespace introduced by the replacements.
        content = re.sub(r"[ \t]+", " ", content).strip()
        return content

    @staticmethod
    def _strip_latex_text_wrapper(content: str) -> str:
        """
        Unwrap LaTeX text-style commands that span the whole boxed content,
        e.g. \\boxed{\\text{Foo Bar}} -> "Foo Bar". Some models (gpt-5.x) add
        these wrappers, which would fail GAIA exact-match scoring. Only strips
        when the wrapper encloses the entire content (verified via balanced
        braces); applied repeatedly for nested wrappers.
        """
        pattern = re.compile(
            r"^\\(?:text|textbf|textit|textrm|texttt|mathrm|mathit)\{(.*)\}$",
            re.DOTALL,
        )
        while True:
            stripped = content.strip()
            m = pattern.match(stripped)
            if not m:
                return content
            inner = m.group(1)
            depth = 0
            for ch in inner:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth < 0:
                        # Closing brace of the wrapper occurs mid-string, e.g.
                        # "\text{a} and \text{b}" — not a pure wrapper, keep as is.
                        return content
            if depth != 0:
                return content
            content = inner

    def format_tool_result_for_user(self, tool_call_execution_result):
        """
        Format tool execution results to be fed back to LLM as user messages.
        Only includes necessary information (results or errors).
        """
        server_name = tool_call_execution_result["server_name"]
        tool_name = tool_call_execution_result["tool_name"]

        if "error" in tool_call_execution_result:
            # Provide concise error information to LLM
            content = f"Tool call to {tool_name} on {server_name} failed. Error: {tool_call_execution_result['error']}"
        elif "result" in tool_call_execution_result:
            # Provide tool's original output results
            content = tool_call_execution_result["result"]
            # Can consider truncating overly long results
            max_len = 100_000  # 100k chars = 25k tokens
            if len(content) > max_len:
                content = content[:max_len] + "\n... [Result truncated]"
        else:
            content = f"Tool call to {tool_name} on {server_name} completed, but produced no specific output or result."

        # Return format suitable as user message content
        # return [{"type": "text", "text": content}]
        return {"type": "text", "text": content}


    @staticmethod
    def _extract_final_conclusion_line(text: str) -> str:
        """Verbatim grab of the FINAL CONCLUSION content (same-line remainder, or the
        next non-empty line). Markdown emphasis/heading glyphs are stripped; anything
        longer than 400 chars is treated as not-an-answer. Returns "" when absent."""
        import re as _re

        m = _re.search(r"FINAL (?:CONCLUSION|ANSWER)\s*:?\s*(.*)", text, _re.IGNORECASE)
        if not m:
            return ""
        candidate = m.group(1).strip()
        if not candidate:
            for line in text[m.end():].splitlines():
                if line.strip():
                    candidate = line.strip()
                    break
        candidate = candidate.strip().strip("`").strip()
        candidate = _re.sub(r"^[#*\s]+|[#*\s]+$", "", candidate)
        candidate = _re.sub(r"\*\*(.+?)\*\*", r"\1", candidate)
        meta = _re.match(
            r"^(.*?)[ \t]*\((?:[^)]*\b(?:candidate|primary|likely|approx\w*|confidence|tentative|guess|uncertain\w*)\b[^)]*)\)$",
            candidate,
            _re.IGNORECASE,
        )
        if meta and meta.group(1).strip():
            candidate = meta.group(1).strip()
        if not candidate or len(candidate) > 400:
            return ""
        return candidate

    def format_final_summary_and_log(self, final_answer_text, client=None):
        """Format final summary information, including answer and token statistics"""
        summary_lines = []
        summary_lines.append("\n" + "=" * 30 + " Final Answer " + "=" * 30)
        summary_lines.append(final_answer_text)

        # Extract boxed result - find the last match using safer regex patterns
        boxed_result = self._extract_boxed_content(final_answer_text)

        # Add extracted result section
        summary_lines.append("\n" + "-" * 20 + " Extracted Result " + "-" * 20)

        if not boxed_result and final_answer_text:
            # Fallback: when the boxed-rewrite step is unavailable (e.g. the extraction
            # LLM is down) and the summary itself skipped \boxed{}, take the model's own
            # FINAL CONCLUSION line verbatim instead of scoring an automatic zero.
            boxed_result = self._extract_final_conclusion_line(final_answer_text)

        if boxed_result:
            summary_lines.append(boxed_result)
        elif final_answer_text:
            summary_lines.append("No \\boxed{} content found.")
            boxed_result = (
                "Final response is generated by LLM, but no \\boxed{} content found."
            )
        else:
            summary_lines.append("No \\boxed{} content found.")
            boxed_result = "No final answer generated."

        return "\n".join(summary_lines), boxed_result
