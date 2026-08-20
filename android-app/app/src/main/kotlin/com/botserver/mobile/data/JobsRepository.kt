package com.botserver.mobile.data

import com.botserver.mobile.data.dto.JobSummary
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class JobsRepository @Inject constructor(private val apiService: ApiService) {
    suspend fun list(status: String? = null): List<JobSummary> = apiService.jobs(status = status)
}
