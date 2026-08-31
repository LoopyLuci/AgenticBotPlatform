package com.botserver.mobile.data

import com.botserver.mobile.data.dto.JobSummary
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class JobsRepository @Inject constructor(
    private val apiService: ApiService,
    private val liveEvents: LiveEventsClient,
) {
    suspend fun list(status: String? = null): List<JobSummary> = apiService.jobs(status = status)

    /** The shared LiveEventsClient's "job_update" pushes, unwrapped — fired
     * on job creation and every status transition (see
     * bot/dashboard/server.py's _on_job_changed()). */
    fun observeLiveUpdates(): Flow<JobSummary> = liveEvents.jobUpdates.map { it.job }
}
