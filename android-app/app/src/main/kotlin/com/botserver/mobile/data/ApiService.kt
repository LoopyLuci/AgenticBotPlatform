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
import com.botserver.mobile.data.dto.NetworkInfoResponse
import com.botserver.mobile.data.dto.OkResponse
import com.botserver.mobile.data.dto.ApkSendAllRequest
import com.botserver.mobile.data.dto.ApkSendAllResponse
import com.botserver.mobile.data.dto.ApkSendRequest
import com.botserver.mobile.data.dto.ApkSendResponse
import com.botserver.mobile.data.dto.MeshRedeemRequest
import com.botserver.mobile.data.dto.MeshRedeemResponse
import com.botserver.mobile.data.dto.PairingListResponse
import com.botserver.mobile.data.dto.PendingApkResponse
import com.botserver.mobile.data.dto.PersonaPreset
import com.botserver.mobile.data.dto.ModelToggleRequest
import com.botserver.mobile.data.dto.ModelTogglePaidRequest
import com.botserver.mobile.data.dto.ModelTogglePaidResponse
import com.botserver.mobile.data.dto.ProviderModelsResponse
import com.botserver.mobile.data.dto.ProvidersCatalogResponse
import com.botserver.mobile.data.dto.ProvidersListResponse
import com.botserver.mobile.data.dto.SetProviderRequest
import com.botserver.mobile.data.dto.ServerChatConversation
import com.botserver.mobile.data.dto.ServerChatMessage
import com.botserver.mobile.data.dto.ServerChatSendRequest
import com.botserver.mobile.data.dto.ServerChatSendResponse
import com.botserver.mobile.data.dto.ServerChatWhoAmI
import com.botserver.mobile.data.dto.RegisterPushTokenRequest
import com.botserver.mobile.data.dto.SendMessageRequest
import com.botserver.mobile.data.dto.SendToBotRequest
import com.botserver.mobile.data.dto.SendToBotResponse
import com.botserver.mobile.data.dto.SessionDetail
import com.botserver.mobile.data.dto.SessionSummary
import com.botserver.mobile.data.dto.SupportBotAskRequest
import com.botserver.mobile.data.dto.SupportBotConfirmRequest
import com.botserver.mobile.data.dto.SupportBotReply
import com.botserver.mobile.data.dto.TurnCredentialsResponse
import com.botserver.mobile.data.dto.UploadCompleteResponse
import com.botserver.mobile.data.dto.UploadInitRequest
import com.botserver.mobile.data.dto.UploadInitResponse
import okhttp3.MultipartBody
import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.PUT
import retrofit2.http.Part
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

    // "Chat with Bot" mode — a real message TO the bot; no chat_id in the
    // request, the server derives the sender's identity from this request's
    // own auth. See bot/dashboard/server.py's api_chat_send_to_bot().
    @POST("/api/chat/send-to-bot")
    suspend fun sendToBot(@Body request: SendToBotRequest): SendToBotResponse

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

    // The server's own live-detected LAN/Tailscale/Funnel addresses (see
    // bot/network_info.py) — polled opportunistically by HostSyncRepository
    // so a paired device's stored hosts self-heal after a LAN IP changes
    // under DHCP or Funnel gets turned on later, without ever re-pairing.
    @GET("/api/network-info")
    suspend fun networkInfo(): NetworkInfoResponse

    // Pull-based APK delivery — see bot/db.py's apk_pushes table comment.
    // The desktop app's "Send APK"/"Send APK to All Paired Devices"
    // buttons queue a push server-side; this device checks for one of its
    // own on its own schedule (there's no reliable way for the server to
    // wake a backgrounded phone without Firebase configured) and, if
    // present, downloads and hands off to UpdateInstaller.
    @GET("/api/android/apk/pending")
    suspend fun pendingApk(): PendingApkResponse

    @Streaming
    @GET("/api/android/apk/download/{pushId}")
    suspend fun downloadApk(@Path("pushId") pushId: Int): ResponseBody

    // This device pushing an update to one/every other paired device — the
    // Devices screen's own "Send" and "Send to all devices" buttons,
    // mirroring the desktop dashboard's Mobile tab.
    @POST("/api/android/apk/send")
    suspend fun sendApk(@Body request: ApkSendRequest): ApkSendResponse

    @POST("/api/android/apk/send-all")
    suspend fun sendApkToAll(@Body request: ApkSendAllRequest = ApkSendAllRequest()): ApkSendAllResponse

    // Called by this device's own MeshServer after accepting an incoming
    // mesh connection, to validate the token the connecting peer presented
    // before streaming this device's APK to it.
    @POST("/api/android/apk/mesh/redeem")
    suspend fun redeemMeshToken(@Body request: MeshRedeemRequest): MeshRedeemResponse

    // Short-lived TURN relay credentials for WebRtcMeshClient's ICE server
    // list (bot/turn.py) — fetched fresh per PeerConnection since a single
    // transfer is far shorter than the credential's ttl. {enabled: false}
    // when no TURN server is configured is the normal "STUN-only" case.
    @GET("/api/turn/credentials")
    suspend fun turnCredentials(): TurnCredentialsResponse

    // ---------------------------------------------------------- server chat
    // A permanent, bot-independent channel between this server's own
    // devices (desktop + every paired phone) — see bot/db.py's
    // server_chat_conversations table comment.
    @GET("/api/server-chat/whoami")
    suspend fun serverChatWhoAmI(): ServerChatWhoAmI

    @GET("/api/server-chat/conversations")
    suspend fun serverChatConversations(): List<ServerChatConversation>

    @GET("/api/server-chat/messages")
    suspend fun serverChatMessages(
        @Query("conversation_id") conversationId: Int,
        @Query("after_id") afterId: Int = 0,
        @Query("limit") limit: Int = 200,
    ): List<ServerChatMessage>

    @POST("/api/server-chat/send")
    suspend fun serverChatSend(@Body request: ServerChatSendRequest): ServerChatSendResponse

    @Multipart
    @POST("/api/server-chat/send-file")
    suspend fun serverChatSendFile(
        @Part("conversation_id") conversationId: RequestBody,
        @Part("text") text: RequestBody,
        @Part file: MultipartBody.Part,
    ): ServerChatSendResponse

    @Streaming
    @GET("/api/server-chat/attachments/{messageId}")
    suspend fun downloadServerChatAttachment(@Path("messageId") messageId: Int): ResponseBody

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

    // ---------------------------------------------------------- pairing ---
    // Someone messaged a bot from an unrecognized chat id and got a
    // one-time code back — see bot/pairing.py. Approving here appends
    // their id into that instance's allowed_user_ids.
    @GET("/api/pairing")
    suspend fun pairing(@Query("instance_id") instanceId: Int? = null): PairingListResponse

    @POST("/api/pairing/{pairingId}/approve")
    suspend fun approvePairing(@Path("pairingId") pairingId: Int): OkResponse

    @POST("/api/pairing/{pairingId}/deny")
    suspend fun denyPairing(@Path("pairingId") pairingId: Int): OkResponse

    // ---------------------------------------------------------- settings --
    // Mirrors the desktop dashboard's Control Center — see ConfigDto.kt for
    // the typed accessors used against ConfigResponse.current.
    @GET("/api/config")
    suspend fun config(): ConfigResponse

    @GET("/api/models")
    suspend fun models(): ModelsResponse

    @GET("/api/personas")
    suspend fun personas(): List<PersonaPreset>

    @POST("/api/config/set")
    suspend fun setConfig(@Body request: ConfigSetRequest): OkResponse

    // -------------------------------------------------------- providers ---
    // Full parity with the desktop dashboard's Providers + Models page —
    // add a named OpenAI-compatible endpoint (local or cloud), browse and
    // toggle its models. See bot/providers.py and bot/models.py's
    // browse_provider_models(). Reachable by a mobile device key exactly
    // like /api/bots — same tier, same reasoning (a lost/unlocked phone
    // could already rewrite bot platform tokens; provider API keys are no
    // more sensitive than that).
    @GET("/api/providers")
    suspend fun providers(): ProvidersListResponse

    @GET("/api/providers/catalog")
    suspend fun providersCatalog(): ProvidersCatalogResponse

    @POST("/api/providers")
    suspend fun setProvider(@Body request: SetProviderRequest): OkResponse

    @DELETE("/api/providers/{name}")
    suspend fun deleteProvider(@Path("name") name: String): OkResponse

    @GET("/api/providers/{name}/models")
    suspend fun providerModels(@Path("name") name: String): ProviderModelsResponse

    @POST("/api/providers/{name}/models/toggle")
    suspend fun toggleModel(@Path("name") name: String, @Body request: ModelToggleRequest): OkResponse

    @POST("/api/providers/{name}/models/toggle-paid")
    suspend fun toggleAllPaidModels(@Path("name") name: String, @Body request: ModelTogglePaidRequest): ModelTogglePaidResponse

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
