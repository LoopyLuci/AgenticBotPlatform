package com.botserver.mobile.ui.sessions

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
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
import com.botserver.mobile.data.dto.SessionSummary
import com.botserver.mobile.ui.components.EmptyState
import com.botserver.mobile.ui.components.ErrorState
import com.botserver.mobile.ui.components.LoadingState
import com.botserver.mobile.ui.components.PullRefreshBox

@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
fun SessionsScreen(viewModel: SessionsViewModel = hiltViewModel()) {
    val state by viewModel.uiState.collectAsState()
    val snackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(Unit) { viewModel.refresh() }
    LaunchedEffect(Unit) {
        viewModel.snackbarMessages.collect { message -> snackbarHostState.showSnackbar(message) }
    }
    var pendingDelete by remember { mutableStateOf<SessionSummary?>(null) }

    Scaffold(
        topBar = { TopAppBar(title = { Text("Sessions") }) },
        snackbarHost = { SnackbarHost(snackbarHostState) },
    ) { padding ->
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
                                onDelete = { pendingDelete = session },
                                modifier = Modifier.animateItemPlacement(),
                            )
                        }
                    }
                }
            }
        }
    }

    pendingDelete?.let { session ->
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("Delete this session?") },
            text = { Text("Every message and job filed under \"${session.title.ifBlank { "Untitled" }}\" will be permanently removed. This can't be undone.") },
            confirmButton = {
                TextButton(onClick = { viewModel.delete(session); pendingDelete = null }) {
                    Text("Delete", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = { TextButton(onClick = { pendingDelete = null }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun SessionRow(session: SessionSummary, onClick: () -> Unit, onDelete: () -> Unit, modifier: Modifier = Modifier) {
    var menuOpen by remember { mutableStateOf(false) }
    val clipboard = LocalClipboardManager.current
    Box(modifier = modifier) {
        Surface(
            shape = RoundedCornerShape(12.dp),
            tonalElevation = 1.dp,
            modifier = Modifier
                .fillMaxWidth()
                // Single gesture detector for both tap and long-press —
                // a separate .clickable + .pointerInput(onLongPress) pair
                // both independently process the same touch, so a real
                // held-then-released tap fires the long-press (opening
                // the menu) AND clickable's own release-triggered onClick
                // (navigating away), burying the menu instantly. Found
                // via real on-device testing on this exact pattern.
                .pointerInput(Unit) {
                    detectTapGestures(onTap = { onClick() }, onLongPress = { menuOpen = true })
                },
        ) {
            Row(
                modifier = Modifier.fillMaxWidth().padding(14.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column {
                    Text(session.title.ifBlank { "Untitled" }, style = MaterialTheme.typography.titleSmall)
                    Text("${session.itemCount} item(s)", style = MaterialTheme.typography.bodySmall)
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(session.lastActivityAt ?: "", style = MaterialTheme.typography.labelSmall)
                    IconButton(onClick = { menuOpen = true }, modifier = Modifier.size(28.dp)) {
                        Icon(Icons.Filled.MoreVert, contentDescription = "Session options", modifier = Modifier.size(16.dp))
                    }
                }
            }
        }
        DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
            DropdownMenuItem(
                text = { Text("Copy title") },
                onClick = {
                    menuOpen = false
                    clipboard.setText(AnnotatedString(session.title.ifBlank { "Untitled" }))
                },
            )
            DropdownMenuItem(
                text = { Text("Delete session") },
                onClick = { menuOpen = false; onDelete() },
            )
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
