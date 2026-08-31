package com.botserver.mobile.data

import android.content.ContentResolver
import android.content.Context
import android.database.Cursor
import android.net.Uri
import android.provider.OpenableColumns
import com.botserver.mobile.data.dto.OkResponse
import com.botserver.mobile.data.dto.UploadInitResponse
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import java.io.ByteArrayInputStream
import kotlinx.coroutines.runBlocking
import okhttp3.RequestBody
import okio.Buffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** ChatRepository.sendFile()'s chunking math — the part of the upload path
 * with real logic worth verifying directly: chunk count/sizes matching
 * init's declared chunkSize, and the pre-flight size check short-circuiting
 * before ever touching the file when the *declared* (cursor-reported) size
 * alone is already over the warn threshold. */
class ChatRepositorySendFileTest {
    @get:Rule
    val tempFolder = TemporaryFolder()

    private lateinit var apiService: ApiService
    private lateinit var context: Context
    private lateinit var resolver: ContentResolver
    private lateinit var uri: Uri
    private lateinit var repository: ChatRepository

    @Before
    fun setUp() {
        apiService = mockk(relaxed = true)
        resolver = mockk()
        uri = mockk()
        context = mockk {
            every { cacheDir } returns tempFolder.newFolder("cache")
            every { contentResolver } returns resolver
        }
        every { resolver.query(uri, null, null, null, null) } returns null
        every { resolver.getType(uri) } returns "application/octet-stream"
        repository = ChatRepository(apiService, mockk(relaxed = true), mockk(relaxed = true), context)
    }

    private fun bodyBytes(body: RequestBody): ByteArray {
        val buffer = Buffer()
        body.writeTo(buffer)
        return buffer.readByteArray()
    }

    @Test
    fun `a file larger than one chunk is split into exactly the right number of chunks`() = runBlocking {
        val totalSize = 25_000
        val chunkSize = 10_000 // 3 chunks: 10000, 10000, 5000
        val fileBytes = ByteArray(totalSize) { (it % 256).toByte() }
        every { resolver.openInputStream(uri) } returns ByteArrayInputStream(fileBytes)
        coEvery { apiService.uploadInit(any()) } returns UploadInitResponse(sessionId = "session-1", chunkSize = chunkSize)

        val chunkSizesSeen = mutableListOf<Int>()
        coEvery { apiService.uploadChunk(any(), any(), any()) } coAnswers {
            chunkSizesSeen += bodyBytes(thirdArg()).size
            OkResponse(ok = true)
        }

        repository.sendFile(instanceId = 1, chatId = "chat", text = "caption", uri = uri)

        assertEquals(listOf(10_000, 10_000, 5_000), chunkSizesSeen)
        coVerify(exactly = 1) { apiService.uploadComplete("session-1") }
    }

    @Test
    fun `a file smaller than one chunk sends exactly one chunk of its own size`() = runBlocking {
        val fileBytes = ByteArray(500) { 1 }
        every { resolver.openInputStream(uri) } returns ByteArrayInputStream(fileBytes)
        coEvery { apiService.uploadInit(any()) } returns UploadInitResponse(sessionId = "session-2", chunkSize = 65536)
        val chunkSizesSeen = mutableListOf<Int>()
        coEvery { apiService.uploadChunk(any(), any(), any()) } coAnswers {
            chunkSizesSeen += bodyBytes(thirdArg()).size
            OkResponse(ok = true)
        }

        repository.sendFile(instanceId = 1, chatId = "chat", text = "", uri = uri)

        assertEquals(listOf(500), chunkSizesSeen)
    }

    @Test
    fun `a declared size over the warn threshold is rejected before reading the file`() {
        val hugeCursor = mockk<Cursor> {
            every { getColumnIndex(OpenableColumns.DISPLAY_NAME) } returns 0
            every { getColumnIndex(OpenableColumns.SIZE) } returns 1
            every { moveToFirst() } returns true
            every { getString(0) } returns "huge.bin"
            every { getLong(1) } returns 3L * 1024 * 1024 * 1024 // 3GB > the 2GB warn threshold
            every { close() } returns Unit
        }
        every { resolver.query(uri, null, null, null, null) } returns hugeCursor

        assertThrows(AttachmentTooLargeException::class.java) {
            runBlocking { repository.sendFile(instanceId = 1, chatId = "chat", text = "", uri = uri) }
        }
        verify(exactly = 0) { resolver.openInputStream(any()) }
    }

    @Test
    fun `an empty file is rejected with a clear error`() {
        every { resolver.openInputStream(uri) } returns ByteArrayInputStream(ByteArray(0))

        val thrown = assertThrows(IllegalArgumentException::class.java) {
            runBlocking { repository.sendFile(instanceId = 1, chatId = "chat", text = "", uri = uri) }
        }
        assertEquals("That file is empty.", thrown.message)
    }
}
