package com.botserver.mobile.ui.support

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.botserver.mobile.ui.components.SlashCommandSuggestions
import kotlinx.coroutines.launch

/** The phone counterpart to the desktop dashboard's Support Bot panel —
 * same server-side classifier and management actions (bot/support_bot/),
 * just a phone-shaped single-pane chat instead of a sidebar section. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SupportBotScreen(viewModel: SupportBotViewModel = hiltViewModel()) {
    val state by viewModel.uiState.collectAsState()
    var menuOpen by remember { mutableStateOf(false) }
    var confirmClearOpen by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Support Bot", fontWeight = FontWeight.Bold) },
                actions = {
                    Box {
                        IconButton(onClick = { menuOpen = true }) {
                            Icon(Icons.Filled.MoreVert, contentDescription = "Chat options")
                        }
                        DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                            DropdownMenuItem(
                                text = { Text("Clear chat") },
                                onClick = { menuOpen = false; confirmClearOpen = true },
                            )
                        }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.surface),
            )
        },
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding)) {
            val listState = rememberLazyListState()
            val scope = rememberCoroutineScope()
            LaunchedEffect(state.messages.size) {
                if (state.messages.isNotEmpty()) scope.launch { listState.animateScrollToItem(state.messages.size - 1) }
            }

            LazyColumn(
                modifier = Modifier.weight(1f).fillMaxWidth().padding(horizontal = 12.dp),
                state = listState,
                verticalArrangement = Arrangement.spacedBy(8.dp),
                contentPadding = PaddingValues(vertical = 12.dp),
            ) {
                items(state.messages, key = { it.id }) { message ->
                    SupportBotBubble(
                        message,
                        onConfirm = { viewModel.confirm(message) },
                        onDelete = { viewModel.deleteMessage(message) },
                    )
                }
            }

            SupportBotComposer(sending = state.sending, onSend = viewModel::send)
        }
    }

    if (confirmClearOpen) {
        AlertDialog(
            onDismissRequest = { confirmClearOpen = false },
            title = { Text("Clear this chat?") },
            text = { Text("This resets the conversation back to the start. This can't be undone.") },
            confirmButton = {
                TextButton(onClick = { confirmClearOpen = false; viewModel.clear() }) {
                    Text("Clear", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = { TextButton(onClick = { confirmClearOpen = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun SupportBotBubble(message: SupportBotMessage, onConfirm: () -> Unit, onDelete: () -> Unit) {
    val isOut = message.direction == "out"
    val bubbleShape = RoundedCornerShape(
        topStart = 16.dp, topEnd = 16.dp,
        bottomStart = if (isOut) 16.dp else 4.dp,
        bottomEnd = if (isOut) 4.dp else 16.dp,
    )
    var menuOpen by remember { mutableStateOf(false) }
    var selectTextOpen by remember { mutableStateOf(false) }
    val clipboard = LocalClipboardManager.current

    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = if (isOut) Arrangement.End else Arrangement.Start) {
        Box {
            Surface(
                color = if (isOut) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant,
                contentColor = if (isOut) Color.White else MaterialTheme.colorScheme.onSurfaceVariant,
                shape = bubbleShape,
                shadowElevation = 1.dp,
                modifier = Modifier
                    .widthIn(max = 280.dp)
                    .pointerInput(Unit) { detectTapGestures(onLongPress = { menuOpen = true }) },
            ) {
                Column(Modifier.padding(horizontal = 13.dp, vertical = 9.dp)) {
                    Row(verticalAlignment = Alignment.Top) {
                        Text(message.text, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f, fill = false))
                        IconButton(onClick = { menuOpen = true }, modifier = Modifier.size(24.dp)) {
                            Icon(
                                Icons.Filled.MoreVert,
                                contentDescription = "Message options",
                                modifier = Modifier.size(14.dp),
                                tint = LocalContentColorFor(isOut),
                            )
                        }
                    }
                    if (message.needsConfirm && !message.confirmResolved) {
                        Spacer(Modifier.height(4.dp))
                        Button(onClick = onConfirm) { Text("Confirm") }
                    }
                }
            }
            DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                DropdownMenuItem(
                    text = { Text("Copy message") },
                    onClick = { clipboard.setText(AnnotatedString(message.text)); menuOpen = false },
                )
                DropdownMenuItem(
                    text = { Text("Select text") },
                    onClick = { menuOpen = false; selectTextOpen = true },
                )
                DropdownMenuItem(
                    text = { Text("Delete message") },
                    onClick = { menuOpen = false; onDelete() },
                )
            }
        }
    }

    if (selectTextOpen) {
        AlertDialog(
            onDismissRequest = { selectTextOpen = false },
            title = { Text("Select text") },
            text = { SelectionContainer { Text(message.text, style = MaterialTheme.typography.bodyMedium) } },
            confirmButton = { TextButton(onClick = { selectTextOpen = false }) { Text("Close") } },
        )
    }
}

@Composable
private fun LocalContentColorFor(isOut: Boolean) =
    (if (isOut) Color.White else MaterialTheme.colorScheme.onSurfaceVariant).copy(alpha = 0.65f)

@Composable
private fun SupportBotComposer(sending: Boolean, onSend: (String) -> Unit) {
    var text by remember { mutableStateOf("") }

    Column(modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp)) {
        SlashCommandSuggestions(text = text, onSelect = { text = it })
        Row(verticalAlignment = Alignment.Bottom) {
            TextField(
                value = text,
                onValueChange = { text = it },
                modifier = Modifier.weight(1f),
                placeholder = { Text("Ask the Support Bot…") },
                maxLines = 4,
                enabled = !sending,
                shape = RoundedCornerShape(24.dp),
                colors = TextFieldDefaults.colors(
                    focusedIndicatorColor = Color.Transparent,
                    unfocusedIndicatorColor = Color.Transparent,
                    disabledIndicatorColor = Color.Transparent,
                    focusedContainerColor = MaterialTheme.colorScheme.surfaceVariant,
                    unfocusedContainerColor = MaterialTheme.colorScheme.surfaceVariant,
                ),
            )
            Spacer(Modifier.width(8.dp))
            FilledIconButton(
                enabled = !sending && text.isNotBlank(),
                shape = RoundedCornerShape(50),
                onClick = { onSend(text); text = "" },
            ) {
                if (sending) CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp, color = Color.White)
                else Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send")
            }
        }
    }
}
