package com.agenticbotplatform.mobile.data

import com.agenticbotplatform.mobile.data.dto.ChatStreamFrame
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatStreamTest {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private fun frame(kind: String, text: String? = null, tool: String? = null, instance: Int? = 1, turn: String = "t1") =
        ChatStreamFrame(instanceId = instance, chatId = "c", turnId = turn, kind = kind, text = text, tool = tool)

    private fun fold(vararg frames: ChatStreamFrame) =
        frames.fold(emptyMap<Int, ChatDraft>()) { acc, f -> ChatDrafts.reduce(acc, f) }

    @Test
    fun `a text_delta frame parses, unknown fields ignored`() {
        val f = json.decodeFromString(
            ChatStreamFrame.serializer(),
            """{"type":"chat_stream","instance_id":3,"chat_id":"app:1","turn_id":"abc","kind":"text_delta","text":"Hi","extra":1}""",
        )
        assertEquals(ChatStreamFrame(3, "app:1", "abc", "text_delta", "Hi", null), f)
    }

    @Test
    fun `a tool and a done frame parse with missing optional fields`() {
        val tool = json.decodeFromString(
            ChatStreamFrame.serializer(),
            """{"type":"chat_stream","instance_id":3,"chat_id":"c","turn_id":"abc","kind":"tool_start","tool":"read_file"}""",
        )
        assertEquals("read_file", tool.tool)
        assertNull(tool.text)
        val done = json.decodeFromString(
            ChatStreamFrame.serializer(),
            """{"type":"chat_stream","instance_id":3,"chat_id":"c","turn_id":"abc","kind":"done"}""",
        )
        assertEquals("done", done.kind)
    }

    @Test
    fun `deltas accumulate in order`() {
        val d = fold(frame("text_delta", "Hel"), frame("text_delta", "lo "), frame("text_delta", "world"))
        assertEquals("Hello world", d[1]?.text)
    }

    @Test
    fun `tool line shows while running and clears on tool_end or new text`() {
        val running = fold(frame("text_delta", "x"), frame("tool_start", tool = "shell"))
        assertEquals("shell", running[1]?.tool)
        assertEquals("x", running[1]?.text)
        assertNull(fold(frame("tool_start", tool = "shell"), frame("tool_end", tool = "shell"))[1]?.tool)
        assertNull(fold(frame("tool_start", tool = "shell"), frame("text_delta", "y"))[1]?.tool)
    }

    @Test
    fun `reset clears the text so far`() {
        val d = fold(frame("text_delta", "old"), frame("reset"), frame("text_delta", "new"))
        assertEquals("new", d[1]?.text)
    }

    @Test
    fun `done replaces the draft - it is removed`() {
        assertTrue(fold(frame("text_delta", "x"), frame("done")).isEmpty())
    }

    @Test
    fun `error ends the draft`() {
        assertTrue(fold(frame("text_delta", "x"), frame("error")).isEmpty())
    }

    @Test
    fun `drafts of other instances are independent and done only ends its own`() {
        val d = fold(
            frame("text_delta", "a", instance = 1),
            frame("text_delta", "b", instance = 2, turn = "t2"),
            frame("done", instance = 1),
        )
        assertEquals(setOf(2), d.keys)
        assertEquals("b", d[2]?.text)
    }

    @Test
    fun `a new turn replaces the old draft instead of appending to it`() {
        val d = fold(frame("text_delta", "old", turn = "t1"), frame("text_delta", "new", turn = "t2"))
        assertEquals(ChatDraft("t2", "new"), d[1])
    }

    @Test
    fun `frames without an instance id or with an unknown kind change nothing`() {
        assertTrue(fold(frame("text_delta", "x", instance = null)).isEmpty())
        val d = fold(frame("text_delta", "x"), frame("something_new"))
        assertEquals("x", d[1]?.text)
    }

    @Test
    fun `no frames means no draft - the final reply path is unchanged`() {
        assertTrue(fold().isEmpty())
        assertTrue(fold(frame("done")).isEmpty())
    }
}
