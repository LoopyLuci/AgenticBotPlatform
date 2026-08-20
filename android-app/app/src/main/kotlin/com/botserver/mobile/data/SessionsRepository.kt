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
}
