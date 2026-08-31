package com.botserver.mobile.ui.jobs

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.botserver.mobile.data.dto.JobSummary
import com.botserver.mobile.ui.components.EmptyState
import com.botserver.mobile.ui.components.ErrorState
import com.botserver.mobile.ui.components.LoadingState

private fun statusColor(status: String) = when (status) {
    "success" -> androidx.compose.ui.graphics.Color(0xFF2E7D32)
    "failed" -> androidx.compose.ui.graphics.Color(0xFFC62828)
    "running" -> androidx.compose.ui.graphics.Color(0xFF1565C0)
    else -> androidx.compose.ui.graphics.Color.Gray
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun JobsScreen(viewModel: JobsViewModel = hiltViewModel()) {
    val state by viewModel.uiState.collectAsState()
    LaunchedEffect(Unit) { viewModel.start() }

    Scaffold(topBar = { TopAppBar(title = { Text("Jobs") }) }) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            if (state.loading && state.jobs.isEmpty()) {
                LoadingState()
            } else if (state.error != null && state.jobs.isEmpty()) {
                ErrorState(state.error!!, onRetry = { viewModel.refreshNow() })
            } else if (state.jobs.isEmpty()) {
                EmptyState("No jobs yet.")
            } else {
                LazyColumn(contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(state.jobs, key = { it.id }) { job -> JobRow(job) }
                }
            }
        }
    }
}

@Composable
private fun JobRow(job: JobSummary) {
    Surface(shape = RoundedCornerShape(12.dp), tonalElevation = 1.dp, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Row(horizontalArrangement = Arrangement.SpaceBetween, modifier = Modifier.fillMaxWidth()) {
                Text(job.actionType, style = MaterialTheme.typography.titleSmall)
                Text(job.status, color = statusColor(job.status), style = MaterialTheme.typography.labelMedium)
            }
            job.prompt?.let { Text(it.take(120), style = MaterialTheme.typography.bodySmall, maxLines = 2) }
            Text("${job.backend} · ${job.createdAt}", style = MaterialTheme.typography.labelSmall)
        }
    }
}
