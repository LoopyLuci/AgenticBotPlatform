package com.agenticbotplatform.mobile.data

import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import retrofit2.HttpException
import retrofit2.Response

class UserFacingErrorTest {
    private fun http(code: Int) = HttpException(Response.error<Any>(code, "".toResponseBody(null)))

    @Test
    fun `transport failures become the unreachable message, never the raw exception text`() {
        val raw = "failed to connect to /192.168.69.145 (port 8791) after 6000ms"
        assertEquals(UserFacingError.UNREACHABLE, UserFacingError.message(ConnectException(raw)))
        assertEquals(UserFacingError.UNREACHABLE, UserFacingError.message(SocketTimeoutException(raw)))
        assertEquals(UserFacingError.UNREACHABLE, UserFacingError.message(IOException(raw)))
        assertFalse(UserFacingError.message(IOException(raw)).contains("192.168"))
    }

    @Test
    fun `a wrapped transport failure is still recognised`() {
        assertEquals(UserFacingError.UNREACHABLE, UserFacingError.message(RuntimeException("x", ConnectException("y"))))
    }

    @Test
    fun `auth failures tell the user to re-pair`() {
        assertEquals(UserFacingError.UNAUTHORIZED, UserFacingError.message(http(401)))
        assertEquals(UserFacingError.UNAUTHORIZED, UserFacingError.message(http(403)))
    }

    @Test
    fun `server errors mention the status`() {
        assertTrue(UserFacingError.message(http(500)).contains("500"))
        assertTrue(UserFacingError.message(http(404)).contains("404"))
    }

    @Test
    fun `unknown errors fall back to their message, then to the fallback`() {
        assertEquals("boom", UserFacingError.message(IllegalStateException("boom")))
        assertEquals("fb", UserFacingError.message(null, "fb"))
        assertEquals("fb", UserFacingError.message(IllegalStateException(" "), "fb"))
    }
}
