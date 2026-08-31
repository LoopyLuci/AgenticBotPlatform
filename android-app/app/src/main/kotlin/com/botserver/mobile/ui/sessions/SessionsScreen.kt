package com.botserver.mobile.ui.sessions

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.botserver.mobile.data.dto.SessionSummary
import com.botserver.mobile.ui.components.EmptyState
import com.botserver.mobile.ui.components.ErrorState
import com.botserver.mobile.ui.components.LoadingState
import com.botserver.mobile.ui.components.PullRefreshBox

@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
fun SessionsScreen(viewModel: SessionsViewModel = hiltViewModel()) {
    val state by viewModel.uiState.collectAsState()
    LaunchedEffect(Unit) { viewModel.refresh() }

    Scaffold(topBar = { TopAppBar(title = { Text("Sessions") }) }) { padding ->
        if (state.selected != null) {
            SessionDetailView(state.selected!!, onBack = { viewModel.closeDetail() }, modifier = Modifier.padding(padding))
            return@Scaffold
        }
        Box(Modifier.fillMaxSize().padding(padding)) {
            when {
                state.loading && state.sessions.isEmpty() -> LoadingState()
                state.error != null && state.sessions.isEmpty() -> ErrorState(state.error!!, onRetry = { viewModel.refresh() })
                state.sessions.isEmpty() -> EmptyState("No sessions yet — start a conversation from the Chat tab.")
                else -> PullRefreshBox(refreshing = state.loading, onRefresh = { viewModel.refresh() }, modifier = Modifier.fillMaxSize()) {
                    LazyColumn(
                        modifier = Modifier.testTag("sessions-list"),
                        contentPadding = PaddingValues(12.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        items(state.sessions, key = { it.sessionIdString() }) { session ->
                            SessionRow(
                                session,
                                onClick = { viewModel.open(session.sessionIdString()) },
                                modifier = Modifier.animateItemPlacement(),
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun SessionRow(session: SessionSummary, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Surface(
        shape = RoundedCornerShape(12.dp),
        tonalElevation = 1.dp,
        modifier = modifier.fillMaxWidth().clickable(onClick = onClick),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(14.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Column {
                Text(session.title.ifBlank { "Untitled" }, style = MaterialTheme.typography.titleSmall)
                Text("${session.itemCount} item(s)", style = MaterialTheme.typography.bodySmall)
            }
            Text(session.lastActivityAt ?: "", style = MaterialTheme.typography.labelSmall)
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SessionDetailView(detail: com.botserver.mobile.data.dto.SessionDetail, onBack: () -> Unit, modifier: Modifier = Modifier) {
    Column(modifier = modifier.fillMaxSize()) {
        TopAppBar(
            title = { Text(detail.session.title.ifBlank { "Session" }) },
            navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") } },
        )
        LazyColumn(contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(detail.messages, key = { "m${it.id}" }) { m ->
                Text("${m.direction}: ${m.text}", style = MaterialTheme.typography.bodyMedium)
            }
            items(detail.jobs, key = { "j${it.id}" }) { j ->
                Text("ask: ${j.prompt ?: ""}", style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
}
