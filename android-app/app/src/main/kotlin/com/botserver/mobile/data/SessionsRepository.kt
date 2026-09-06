package com.botserver.mobile.data

import com.botserver.mobile.data.dto.SessionDetail
import com.botserver.mobile.data.dto.SessionSummary
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class SessionsRepository @Inject constructor(private val apiService: ApiService) {
    suspend fun list(instanceId: Int? = null, query: String? = null): List<SessionSummary> =
        apiService.sessions(instanceId = instanceId, q = query)

    suspend fun detail(sessionId: String): SessionDetail = apiService.sessionDetail(sessionId)

    /** Permanently deletes a session and every message/job filed under
     * it (see bot/db.py's delete_session) — this backend route already
     * existed for the desktop dashboard; this is just the first Android
     * wiring for it. */
    suspend fun delete(sessionId: String) {
        apiService.deleteSession(sessionId)
    }
}
