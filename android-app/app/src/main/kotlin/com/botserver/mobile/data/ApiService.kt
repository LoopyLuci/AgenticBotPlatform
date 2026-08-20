package com.botserver.mobile.data

import com.botserver.mobile.data.dto.BotInstance
import com.botserver.mobile.data.dto.BotWriteRequest
import com.botserver.mobile.data.dto.ChatMessage
import com.botserver.mobile.data.dto.ChatRecipientsResponse
import com.botserver.mobile.data.dto.ConfigResponse
import com.botserver.mobile.data.dto.ConfigSetRequest
import com.botserver.mobile.data.dto.CreateMobileKeyRequest
import com.botserver.mobile.data.dto.CreateMobileKeyResponse
import com.botserver.mobile.data.dto.DeviceInfo
import com.botserver.mobile.data.dto.JobSummary
import com.botserver.mobile.data.dto.ModelsResponse
import com.botserver.mobile.data.dto.OkResponse
import com.botserver.mobile.data.dto.RegisterPushTokenRequest
import com.botserver.mobile.data.dto.SendMessageRequest
import com.botserver.mobile.data.dto.SessionDetail
import com.botserver.mobile.data.dto.SessionSummary
import com.botserver.mobile.data.dto.SupportBotAskRequest
import com.botserver.mobile.data.dto.SupportBotConfirmRequest
import com.botserver.mobile.data.dto.SupportBotReply
import com.botserver.mobile.data.dto.UploadCompleteResponse
import com.botserver.mobile.data.dto.UploadInitRequest
import com.botserver.mobile.data.dto.UploadInitResponse
import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.PUT
import retrofit2.http.Path
import retrofit2.http.Query
import retrofit2.http.Streaming

/** Retrofit mirror of the dashboard REST API's chat routes
 * (bot/dashboard/server.py) — same shapes the dashboard.html/main.js
 * frontends already consume, just typed here instead of loosely-typed JS. */
interface ApiService {

    @GET("/api/chat/recipients")
    suspend fun chatRecipients(): ChatRecipientsResponse

    @GET("/api/chat/messages")
    suspend fun chatMessages(
        @Query("instance_id") instanceId: Int,
        @Query("after_id") afterId: Int? = null,
        @Query("limit") limit: Int = 100,
    ): List<ChatMessage>

    @POST("/api/chat/send")
    suspend fun sendMessage(@Body request: SendMessageRequest): OkResponse

    @Streaming
    @GET("/api/chat/attachments/{messageId}")
    suspend fun downloadAttachment(@Path("messageId") messageId: Int): ResponseBody

    // Chunked upload — every attachment send goes through this (init → one
    // PUT per chunk → complete) so it can report progress and isn't a
    // single all-or-nothing request. Thumbnails are loaded directly by
    // Coil against /api/chat/attachments/{id}/thumbnail (see
    // ChatRepository.thumbnailUrl()) rather than through Retrofit, since
    // an image loader needs a URL/model, not a suspend call.
    // See bot/dashboard/server.py's /api/uploads/* and ChatRepository.sendFile().
    @POST("/api/uploads/init")
    suspend fun uploadInit(@Body request: UploadInitRequest): UploadInitResponse

    @PUT("/api/uploads/{sessionId}/chunk/{index}")
    suspend fun uploadChunk(
        @Path("sessionId") sessionId: String,
        @Path("index") index: Int,
        @Body chunk: RequestBody,
    ): OkResponse

    @POST("/api/uploads/{sessionId}/complete")
    suspend fun uploadComplete(@Path("sessionId") sessionId: String): UploadCompleteResponse

    @GET("/api/devices")
    suspend fun devices(): List<DeviceInfo>

    @GET("/api/sessions")
    suspend fun sessions(
        @Query("instance_id") instanceId: Int? = null,
        @Query("q") q: String? = null,
        @Query("limit") limit: Int = 50,
    ): List<SessionSummary>

    @GET("/api/sessions/{sessionId}")
    suspend fun sessionDetail(@Path("sessionId") sessionId: String): SessionDetail

    @GET("/api/jobs")
    suspend fun jobs(
        @Query("status") status: String? = null,
        @Query("limit") limit: Int = 50,
    ): List<JobSummary>

    @GET("/api/bots")
    suspend fun bots(): List<BotInstance>

    @GET("/api/bots/{instanceId}")
    suspend fun bot(@Path("instanceId") instanceId: Int): BotInstance

    // Full parity with the desktop dashboard's Bots tab — see
    // bot/dashboard/server.py's _identify_caller() docstring for why these
    // (unlike most reads) are reachable by a mobile device key at all: a
    // deliberate choice, not an oversight.
    @POST("/api/bots")
    suspend fun createBot(@Body request: BotWriteRequest): OkResponse

    @PUT("/api/bots/{instanceId}")
    suspend fun updateBot(@Path("instanceId") instanceId: Int, @Body request: BotWriteRequest): OkResponse

    @DELETE("/api/bots/{instanceId}")
    suspend fun deleteBot(@Path("instanceId") instanceId: Int): OkResponse

    @POST("/api/bots/{instanceId}/enable")
    suspend fun enableBot(@Path("instanceId") instanceId: Int): OkResponse

    @POST("/api/bots/{instanceId}/disable")
    suspend fun disableBot(@Path("instanceId") instanceId: Int): OkResponse

    @POST("/api/bots/{instanceId}/start")
    suspend fun startBot(@Path("instanceId") instanceId: Int): OkResponse

    @POST("/api/bots/{instanceId}/stop")
    suspend fun stopBot(@Path("instanceId") instanceId: Int): OkResponse

    @POST("/api/bots/{instanceId}/restart")
    suspend fun restartBot(@Path("instanceId") instanceId: Int): OkResponse

    // ---------------------------------------------------------- settings --
    // Mirrors the desktop dashboard's Control Center — see ConfigDto.kt for
    // the typed accessors used against ConfigResponse.current.
    @GET("/api/config")
    suspend fun config(): ConfigResponse

    @GET("/api/models")
    suspend fun models(): ModelsResponse

    @POST("/api/config/set")
    suspend fun setConfig(@Body request: ConfigSetRequest): OkResponse

    @POST("/api/push/register")
    suspend fun registerPushToken(@Body request: RegisterPushTokenRequest): OkResponse

    @POST("/api/mobile-keys")
    suspend fun createMobileKey(@Body request: CreateMobileKeyRequest): CreateMobileKeyResponse

    // ------------------------------------------------------- support bot --
    // The local, dependency-free management assistant (bot/support_bot/) —
    // same auth tier as /api/bots and /api/config/set, so it's reachable by
    // a mobile device key exactly like the desktop dashboard's own token.
    @POST("/api/support-bot/ask")
    suspend fun supportBotAsk(@Body request: SupportBotAskRequest): SupportBotReply

    @POST("/api/support-bot/confirm")
    suspend fun supportBotConfirm(@Body request: SupportBotConfirmRequest): SupportBotReply
}
