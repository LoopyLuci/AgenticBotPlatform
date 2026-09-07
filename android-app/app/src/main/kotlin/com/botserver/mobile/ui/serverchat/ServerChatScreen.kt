package com.botserver.mobile.ui.serverchat

import android.content.Intent
import android.net.Uri
import android.webkit.MimeTypeMap
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.InsertDriveFile
import androidx.compose.material.icons.filled.AttachFile
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Send
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import androidx.hilt.navigation.compose.hiltViewModel
import com.botserver.mobile.data.dto.ServerChatConversation
import com.botserver.mobile.data.dto.ServerChatMessage
import kotlinx.coroutines.launch
import java.io.File

private val ServerChatBubbleGradient @Composable get() = Brush.linearGradient(
    listOf(MaterialTheme.colorScheme.tertiary, MaterialTheme.colorScheme.secondary),
)

private fun initialsFor(name: String): String =
    name.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }.take(2)
        .map { it.first().uppercaseChar() }.joinToString("").ifEmpty { "?" }

/** A permanent, bot-independent channel between this server's own devices
 * — the desktop app and every paired phone. Same list → conversation
 * navigation as ChatScreen, but every "conversation" here is another
 * device (or the shared "Server Chat" group room) instead of a bot. */
@Composable
fun ServerChatScreen(viewModel: ServerChatViewModel = hiltViewModel(), peerDeviceIdToOpen: Int? = null) {
    val context = LocalContext.current
    val conversations by viewModel.conversations.collectAsState()
    val activeId by viewModel.activeConversationId.collectAsState()
    val messages by viewModel.messages.collectAsState()
    val myDeviceId by viewModel.myDeviceId.collectAsState()
    val loadError by viewModel.loadError.collectAsState()
    LaunchedEffect(Unit) { viewModel.start() }

    // Arrives from Devices' "Message this device" action — that screen's
    // own pre-flight open already confirmed the conversation exists (or
    // was just recreated after a prior full delete), but it runs on a
    // separate ViewModel instance scoped to a different nav back-stack
    // entry, so it has no way to actually open it here. Re-opening by
    // peer device id (not conversation id) both fetches this device's
    // conversations and switches straight into the right one.
    LaunchedEffect(peerDeviceIdToOpen) {
        if (peerDeviceIdToOpen != null) viewModel.openConversationWith(peerDeviceIdToOpen)
    }

    LaunchedEffect(activeId) {
        while (activeId != null) {
            kotlinx.coroutines.delay(2000)
            viewModel.refreshMessages()
        }
    }
    LaunchedEffect(Unit) {
        while (true) {
            kotlinx.coroutines.delay(15000)
            viewModel.refreshConversations()
        }
    }

    BackHandler(enabled = activeId != null) { viewModel.closeConversation() }

    if (activeId == null) {
        ServerChatListScreen(
            conversations = conversations,
            loadError = loadError,
            onSelect = { viewModel.openConversation(it) },
            onDelete = { viewModel.deleteConversation(it) },
            snackbarMessages = viewModel.snackbarMessages,
        )
    } else {
        val active = conversations.find { it.id == activeId }
        ServerChatConversationScreen(
            context = context,
            conversation = active,
            messages = messages,
            myDeviceId = myDeviceId,
            onBack = { viewModel.closeConversation() },
            onSend = { viewModel.send(it) },
            onSendFile = { uri, caption -> viewModel.sendFile(uri, caption) },
            onDownload = { messageId, name -> viewModel.downloadAttachment(messageId, name) },
            onDeleteMessage = { viewModel.deleteMessage(it) },
            onClearChat = { active?.let { viewModel.deleteConversation(it) } },
            isGroup = active?.kind == "group",
            snackbarMessages = viewModel.snackbarMessages,
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ServerChatListScreen(
    conversations: List<ServerChatConversation>,
    loadError: String?,
    onSelect: (Int) -> Unit,
    onDelete: (ServerChatConversation) -> Unit,
    snackbarMessages: kotlinx.coroutines.flow.SharedFlow<String>,
) {
    val snackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(Unit) {
        snackbarMessages.collect { message -> snackbarHostState.showSnackbar(message) }
    }
    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) },
        topBar = {
            CenterAlignedTopAppBar(
                title = { Text("Server Chat", fontWeight = FontWeight.Bold) },
                colors = TopAppBarDefaults.centerAlignedTopAppBarColors(containerColor = MaterialTheme.colorScheme.surface),
            )
        },
    ) { padding ->
        if (conversations.isEmpty()) {
            Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                Text(
                    loadError ?: "Loading…",
                    modifier = Modifier.padding(24.dp),
                    textAlign = TextAlign.Center,
                )
            }
            return@Scaffold
        }
        LazyColumn(modifier = Modifier.fillMaxSize().padding(padding)) {
            items(conversations, key = { it.id }) { conv ->
                ServerChatListRow(conv, onClick = { onSelect(conv.id) }, onDelete = { onDelete(conv) })
            }
        }
    }
}

@Composable
private fun ServerChatListRow(conv: ServerChatConversation, onClick: () -> Unit, onDelete: () -> Unit) {
    var menuOpen by remember { mutableStateOf(false) }
    var confirmDeleteOpen by remember { mutableStateOf(false) }
    val isGroup = conv.kind == "group"

    Box {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                // A single gesture detector for both tap and long-press —
                // see ChatScreen.kt's ChatListRow for why a separate
                // .clickable + .pointerInput(onLongPress) pair doesn't
                // work: both independently process the same touch, so
                // the long-press-opened menu was immediately buried by
                // clickable's own release-triggered onClick navigating
                // away. Confirmed via real on-device testing.
                .pointerInput(Unit) {
                    detectTapGestures(onTap = { onClick() }, onLongPress = { menuOpen = true })
                }
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier.size(50.dp).clip(CircleShape).background(ServerChatBubbleGradient),
                contentAlignment = Alignment.Center,
            ) {
                Text(initialsFor(conv.title), color = Color.White, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.titleMedium)
            }
            Spacer(Modifier.width(14.dp))
            Column(Modifier.weight(1f)) {
                Text(conv.title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                val preview = conv.lastMessage?.let { it.text?.takeIf { t -> t.isNotBlank() } ?: it.attachmentName?.let { n -> "📎 $n" } }
                Text(
                    preview ?: "No messages yet",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f),
                )
            }
            // Tap-based fallback into the same menu long-press opens —
            // see ChatScreen.kt's MessageBubble for why (some devices'
            // touch handling makes long-press unreliable).
            IconButton(onClick = { menuOpen = true }, modifier = Modifier.size(28.dp)) {
                Icon(
                    Icons.Filled.MoreVert,
                    contentDescription = "Chat options",
                    modifier = Modifier.size(18.dp),
                    tint = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.5f),
                )
            }
        }
        DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
            DropdownMenuItem(
                text = { Text(if (isGroup) "Clear chat" else "Delete chat") },
                onClick = { menuOpen = false; confirmDeleteOpen = true },
            )
        }
    }

    if (confirmDeleteOpen) {
        AlertDialog(
            onDismissRequest = { confirmDeleteOpen = false },
            title = { Text(if (isGroup) "Clear this chat?" else "Delete this chat?") },
            text = {
                Text(
                    if (isGroup) {
                        "Every message in the shared Server Chat room will be permanently removed for everyone. This can't be undone."
                    } else {
                        "This conversation and every message in it will be permanently removed. You can message this device again later to start a new one. This can't be undone."
                    },
                )
            },
            confirmButton = {
                TextButton(onClick = { confirmDeleteOpen = false; onDelete() }) {
                    Text(if (isGroup) "Clear" else "Delete", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = { TextButton(onClick = { confirmDeleteOpen = false }) { Text("Cancel") } },
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ServerChatConversationScreen(
    context: android.content.Context,
    conversation: ServerChatConversation?,
    messages: List<ServerChatMessage>,
    myDeviceId: Int,
    onBack: () -> Unit,
    onSend: (String) -> Unit,
    onSendFile: (Uri, String) -> Unit,
    onDownload: suspend (Int, String) -> File,
    onDeleteMessage: (ServerChatMessage) -> Unit,
    onClearChat: () -> Unit,
    isGroup: Boolean,
    snackbarMessages: kotlinx.coroutines.flow.SharedFlow<String>,
) {
    var menuOpen by remember { mutableStateOf(false) }
    var confirmClearOpen by remember { mutableStateOf(false) }
    val snackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(Unit) {
        snackbarMessages.collect { message -> snackbarHostState.showSnackbar(message) }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) },
        topBar = {
            TopAppBar(
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back to Server Chat")
                    }
                },
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(
                            modifier = Modifier.size(34.dp).clip(CircleShape).background(ServerChatBubbleGradient),
                            contentAlignment = Alignment.Center,
                        ) {
                            Text(initialsFor(conversation?.title ?: "?"), color = Color.White, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.labelLarge)
                        }
                        Spacer(Modifier.width(10.dp))
                        Text(conversation?.title ?: "", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                    }
                },
                actions = {
                    Box {
                        IconButton(onClick = { menuOpen = true }) {
                            Icon(Icons.Filled.MoreVert, contentDescription = "Chat options")
                        }
                        DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                            DropdownMenuItem(
                                text = { Text("Export chat") },
                                onClick = {
                                    menuOpen = false
                                    val file = exportServerChatFile(context, conversation?.title ?: "server-chat", messages, myDeviceId)
                                    shareServerChatExportFile(context, file)
                                },
                            )
                            DropdownMenuItem(
                                text = { Text(if (isGroup) "Clear chat" else "Delete chat") },
                                onClick = {
                                    menuOpen = false
                                    confirmClearOpen = true
                                },
                            )
                        }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.surface),
            )
        },
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding).background(MaterialTheme.colorScheme.background)) {
            val listState = rememberLazyListState()
            val scope = rememberCoroutineScope()
            LaunchedEffect(messages.size) {
                if (messages.isNotEmpty()) scope.launch { listState.animateScrollToItem(messages.size - 1) }
            }
            LazyColumn(
                modifier = Modifier.weight(1f).fillMaxWidth().padding(horizontal = 12.dp),
                state = listState,
                verticalArrangement = Arrangement.spacedBy(8.dp),
                contentPadding = PaddingValues(vertical = 12.dp),
            ) {
                items(messages, key = { it.id }) { message ->
                    val isOut = message.senderDeviceId == myDeviceId
                    ServerChatBubble(
                        message = message,
                        isOut = isOut,
                        onOpen = {
                            scope.launch {
                                runCatching { onDownload(message.id, message.attachmentName ?: "file") }
                                    .onSuccess { file -> openFile(context, file, message.attachmentMime) }
                            }
                        },
                        onDelete = if (isOut) ({ onDeleteMessage(message) }) else null,
                        onExportChat = {
                            val file = exportServerChatFile(context, conversation?.title ?: "server-chat", messages, myDeviceId)
                            shareServerChatExportFile(context, file)
                        },
                    )
                }
            }
            ServerChatComposer(onSend = onSend, onSendFile = onSendFile)
        }
    }

    if (confirmClearOpen) {
        AlertDialog(
            onDismissRequest = { confirmClearOpen = false },
            title = { Text(if (isGroup) "Clear this chat?" else "Delete this chat?") },
            text = {
                Text(
                    if (isGroup) {
                        "Every message in the shared Server Chat room will be permanently removed for everyone. This can't be undone."
                    } else {
                        "This conversation and every message in it will be permanently removed. You can message this device again later to start a new one. This can't be undone."
                    },
                )
            },
            confirmButton = {
                TextButton(onClick = { confirmClearOpen = false; onClearChat() }) {
                    Text(if (isGroup) "Clear" else "Delete", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = { TextButton(onClick = { confirmClearOpen = false }) { Text("Cancel") } },
        )
    }
}

private fun openFile(context: android.content.Context, file: File, mime: String?) {
    val resolvedMime = mime ?: MimeTypeMap.getSingleton().getMimeTypeFromExtension(file.extension) ?: "*/*"
    val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
    val intent = Intent(Intent.ACTION_VIEW).apply {
        setDataAndType(uri, resolvedMime)
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    }
    runCatching { context.startActivity(intent) }
}

@Composable
private fun ServerChatBubble(
    message: ServerChatMessage,
    isOut: Boolean,
    onOpen: () -> Unit,
    onDelete: (() -> Unit)?,
    onExportChat: () -> Unit,
) {
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
                    if (message.text.isNotBlank()) {
                        Text(message.text, style = MaterialTheme.typography.bodyMedium)
                    }
                    if (message.attachmentPath != null) {
                        AssistChip(
                            onClick = onOpen,
                            modifier = Modifier.padding(top = 4.dp),
                            leadingIcon = { Icon(Icons.AutoMirrored.Filled.InsertDriveFile, contentDescription = null, modifier = Modifier.size(16.dp)) },
                            label = { Text("📎 " + (message.attachmentName ?: "file")) },
                        )
                    }
                    // Tap-based fallback into the same menu long-press
                    // opens — see MessageBubble's identical addition in
                    // ui/chat/ChatScreen.kt for why (some devices' touch
                    // handling makes long-press unreliable).
                    Row(
                        modifier = Modifier.fillMaxWidth().padding(top = 2.dp),
                        horizontalArrangement = if (isOut) Arrangement.End else Arrangement.Start,
                    ) {
                        IconButton(onClick = { menuOpen = true }, modifier = Modifier.size(24.dp)) {
                            Icon(
                                Icons.Filled.MoreVert,
                                contentDescription = "Message options",
                                modifier = Modifier.size(14.dp),
                                tint = LocalContentColor.current.copy(alpha = 0.65f),
                            )
                        }
                    }
                }
            }

            DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                if (message.text.isNotBlank()) {
                    DropdownMenuItem(
                        text = { Text("Copy message") },
                        onClick = {
                            clipboard.setText(AnnotatedString(message.text))
                            menuOpen = false
                        },
                    )
                    DropdownMenuItem(
                        text = { Text("Select text") },
                        onClick = { menuOpen = false; selectTextOpen = true },
                    )
                }
                if (onDelete != null) {
                    DropdownMenuItem(
                        text = { Text("Delete message") },
                        onClick = { menuOpen = false; onDelete() },
                    )
                }
                DropdownMenuItem(
                    text = { Text("Export chat") },
                    onClick = { menuOpen = false; onExportChat() },
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
private fun ServerChatComposer(onSend: (String) -> Unit, onSendFile: (Uri, String) -> Unit) {
    var text by remember { mutableStateOf("") }
    var pickedUri by remember { mutableStateOf<Uri?>(null) }
    val pickFile = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri -> pickedUri = uri }

    Column(modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp)) {
        pickedUri?.let { uri ->
            AssistChip(
                onClick = { pickedUri = null },
                label = { Text(uri.lastPathSegment ?: "file") },
                trailingIcon = { Icon(Icons.Filled.Close, contentDescription = "Remove attachment", modifier = Modifier.size(16.dp)) },
                modifier = Modifier.padding(bottom = 6.dp),
            )
        }
        Row(verticalAlignment = Alignment.Bottom) {
            IconButton(onClick = { pickFile.launch("*/*") }) {
                Icon(Icons.Filled.AttachFile, contentDescription = "Attach a file")
            }
            TextField(
                value = text,
                onValueChange = { text = it },
                modifier = Modifier.weight(1f),
                placeholder = { Text("Message…") },
                maxLines = 4,
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
                enabled = text.isNotBlank() || pickedUri != null,
                shape = RoundedCornerShape(50),
                onClick = {
                    val uri = pickedUri
                    if (uri != null) {
                        onSendFile(uri, text)
                        pickedUri = null
                    } else {
                        onSend(text)
                    }
                    text = ""
                },
            ) {
                Icon(Icons.Filled.Send, contentDescription = "Send")
            }
        }
    }
}
