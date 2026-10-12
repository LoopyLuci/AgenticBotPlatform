package com.agenticbotplatform.mobile.data

import com.agenticbotplatform.mobile.data.dto.ChatStreamFrame

/** The live, not-yet-stored reply of one native-agent turn: the text so far
 * and the tool it is running right now (null when none). */
data class ChatDraft(val turnId: String, val text: String = "", val tool: String? = null)

/** Folds "chat_stream" frames (bot/dashboard/chat_stream.py) into one draft
 * per bot instance. Pure, so it is unit-testable without a socket. */
object ChatDrafts {
    /** Applies [frame] to [drafts] (keyed by instance id). A "done" or "error"
     * frame ends that instance's draft; a frame for a new turn replaces an
     * older draft. Frames without a usable instance id are ignored. */
    fun reduce(drafts: Map<Int, ChatDraft>, frame: ChatStreamFrame): Map<Int, ChatDraft> {
        val id = frame.instanceId ?: return drafts
        if (frame.kind == "done" || frame.kind == "error") {
            return if (id in drafts) drafts - id else drafts
        }
        val current = drafts[id]?.takeIf { it.turnId == frame.turnId } ?: ChatDraft(frame.turnId)
        val next = when (frame.kind) {
            "text_delta" -> current.copy(text = current.text + (frame.text ?: ""), tool = null)
            "reset" -> current.copy(text = "", tool = null)
            "tool_start" -> current.copy(tool = frame.tool?.ifBlank { null } ?: "tool")
            "tool_end" -> current.copy(tool = null)
            else -> return drafts
        }
        return drafts + (id to next)
    }
}
