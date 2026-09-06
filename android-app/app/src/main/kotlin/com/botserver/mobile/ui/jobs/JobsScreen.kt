package com.botserver.mobile.ui.jobs

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.botserver.mobile.data.dto.JobSummary
import com.botserver.mobile.ui.components.EmptyState
import com.botserver.mobile.ui.components.ErrorState
import com.botserver.mobile.ui.components.LoadingState
import com.botserver.mobile.ui.components.PullRefreshBox

private fun statusColor(status: String) = when (status) {
    "success" -> androidx.compose.ui.graphics.Color(0xFF2E7D32)
    "failed" -> androidx.compose.ui.graphics.Color(0xFFC62828)
    "running" -> androidx.compose.ui.graphics.Color(0xFF1565C0)
    else -> androidx.compose.ui.graphics.Color.Gray
}

@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
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
                PullRefreshBox(refreshing = state.loading, onRefresh = { viewModel.refreshNow() }, modifier = Modifier.fillMaxSize()) {
                    LazyColumn(
                        modifier = Modifier.testTag("jobs-list"),
                        contentPadding = PaddingValues(12.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        items(state.jobs, key = { it.id }) { job -> JobRow(job, modifier = Modifier.animateItemPlacement()) }
                    }
                }
            }
        }
    }
}

@Composable
private fun JobRow(job: JobSummary, modifier: Modifier = Modifier) {
    var menuOpen by remember { mutableStateOf(false) }
    val clipboard = LocalClipboardManager.current
    Box(modifier = modifier) {
        Surface(
            shape = RoundedCornerShape(12.dp),
            tonalElevation = 1.dp,
            modifier = Modifier
                .fillMaxWidth()
                .pointerInput(Unit) { detectTapGestures(onLongPress = { menuOpen = true }) },
        ) {
            Column(Modifier.padding(14.dp)) {
                Row(horizontalArrangement = Arrangement.SpaceBetween, modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text(job.actionType, style = MaterialTheme.typography.titleSmall)
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(job.status, color = statusColor(job.status), style = MaterialTheme.typography.labelMedium)
                        IconButton(onClick = { menuOpen = true }, modifier = Modifier.size(28.dp)) {
                            Icon(Icons.Filled.MoreVert, contentDescription = "Job options", modifier = Modifier.size(16.dp))
                        }
                    }
                }
                job.prompt?.let { Text(it.take(120), style = MaterialTheme.typography.bodySmall, maxLines = 2) }
                Text("${job.backend} · ${job.createdAt}", style = MaterialTheme.typography.labelSmall)
            }
        }
        DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
            job.prompt?.takeIf { it.isNotBlank() }?.let { prompt ->
                DropdownMenuItem(
                    text = { Text("Copy prompt") },
                    onClick = { clipboard.setText(AnnotatedString(prompt)); menuOpen = false },
                )
            }
            job.result?.takeIf { it.isNotBlank() }?.let { result ->
                DropdownMenuItem(
                    text = { Text("Copy result") },
                    onClick = { clipboard.setText(AnnotatedString(result)); menuOpen = false },
                )
            }
            job.error?.takeIf { it.isNotBlank() }?.let { error ->
                DropdownMenuItem(
                    text = { Text("Copy error") },
                    onClick = { clipboard.setText(AnnotatedString(error)); menuOpen = false },
                )
            }
        }
    }
}
