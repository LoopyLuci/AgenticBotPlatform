package com.botserver.mobile.ui.chat

import android.content.Intent
import android.net.Uri
import android.webkit.MimeTypeMap
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.AttachFile
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.InsertDriveFile
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import androidx.hilt.navigation.compose.hiltViewModel
import coil.compose.AsyncImage
import com.botserver.mobile.data.dto.BotInstanceSummary
import com.botserver.mobile.data.dto.ChatMessage
import com.botserver.mobile.di.PLACEHOLDER_BASE_URL
import com.botserver.mobile.ui.components.SlashCommandSuggestions
import kotlinx.coroutines.launch

private val TgBubbleGradient @Composable get() = Brush.linearGradient(
    listOf(MaterialTheme.colorScheme.primary, MaterialTheme.colorScheme.secondary),
)

private fun initialsFor(name: String): String =
    name.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }.take(2)
        .map { it.first().uppercaseChar() }.joinToString("").ifEmpty { "?" }

/** Real Telegram navigation, not a tab row: a chat list is the landing
 * screen (one row per bot instance — the closest thing this app has to a
 * "conversation"), and tapping a row pushes a full-screen conversation with
 * its own back-arrow header. Mirrors the same list → conversation split the
 * desktop dashboard's Chat tab now uses. */
@Composable
fun ChatScreen(viewModel: ChatViewModel = hiltViewModel()) {
    val context = LocalContext.current
    val state by viewModel.uiState.collectAsState()
    LaunchedEffect(Unit) { viewModel.start() }
    var showConversation by rememberSaveable { mutableStateOf(false) }
    BackHandler(enabled = showConversation) { showConversation = false }

    if (state.instances.isEmpty()) {
        Scaffold(topBar = { ChatListTopBar() }) { padding ->
            Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                Text(
                    state.loadError ?: "No paired bots yet — add one in the desktop dashboard's Bots tab.",
                    modifier = Modifier.padding(24.dp),
                    textAlign = TextAlign.Center,
                )
            }
        }
        return
    }

    if (!showConversation) {
        ChatListScreen(
            instances = state.instances,
            onSelect = { id ->
                viewModel.switchInstance(id)
                showConversation = true
            },
        )
    } else {
        val active = state.instances.find { it.id == state.activeInstanceId }
        ConversationScreen(
            context = context,
            instance = active,
            state = state,
            onBack = { showConversation = false },
            viewModel = viewModel,
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ChatListTopBar() {
    CenterAlignedTopAppBar(
        title = { Text("Chats", fontWeight = FontWeight.Bold) },
        colors = TopAppBarDefaults.centerAlignedTopAppBarColors(
            containerColor = MaterialTheme.colorScheme.surface,
        ),
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ChatListScreen(instances: List<BotInstanceSummary>, onSelect: (Int) -> Unit) {
    Scaffold(topBar = { ChatListTopBar() }) { padding ->
        LazyColumn(modifier = Modifier.fillMaxSize().padding(padding)) {
            items(instances, key = { it.id }) { inst ->
                ChatListRow(inst, onClick = { onSelect(inst.id) })
            }
        }
    }
}

@Composable
private fun ChatListRow(inst: BotInstanceSummary, onClick: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(50.dp)
                .clip(CircleShape)
                .background(TgBubbleGradient),
            contentAlignment = Alignment.Center,
        ) {
            Text(initialsFor(inst.name), color = Color.White, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.titleMedium)
        }
        Spacer(Modifier.width(14.dp))
        Column(Modifier.weight(1f)) {
            Text(inst.name, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Text(
                inst.platform.replaceFirstChar { it.uppercase() } + if (inst.connected) "" else " · offline",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f),
            )
        }
        Box(
            modifier = Modifier
                .size(10.dp)
                .clip(CircleShape)
                .background(if (inst.connected) Color(0xFF3DD65B) else MaterialTheme.colorScheme.outline),
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ConversationScreen(
    context: android.content.Context,
    instance: BotInstanceSummary?,
    state: ChatUiState,
    onBack: () -> Unit,
    viewModel: ChatViewModel,
) {
    Scaffold(
        topBar = {
            TopAppBar(
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back to chats")
                    }
                },
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(
                            modifier = Modifier.size(34.dp).clip(CircleShape).background(TgBubbleGradient),
                            contentAlignment = Alignment.Center,
                        ) {
                            Text(initialsFor(instance?.name ?: "?"), color = Color.White, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.labelLarge)
                        }
                        Spacer(Modifier.width(10.dp))
                        Column {
                            Text(instance?.name ?: "", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                            Text(
                                if (instance?.connected == true) "online" else "not running",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f),
                            )
                        }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.surface),
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .background(MaterialTheme.colorScheme.background),
        ) {
            val activePanel = state.panels[state.activeInstanceId]
            val listState = rememberLazyListState()
            val scope = rememberCoroutineScope()

            LaunchedEffect(activePanel?.messages?.size) {
                val count = activePanel?.messages?.size ?: 0
                if (count > 0) scope.launch { listState.animateScrollToItem(count - 1) }
            }

            LazyColumn(
                modifier = Modifier.weight(1f).fillMaxWidth().padding(horizontal = 12.dp),
                state = listState,
                verticalArrangement = Arrangement.spacedBy(8.dp),
                contentPadding = PaddingValues(vertical = 12.dp),
            ) {
                items(activePanel?.messages.orEmpty(), key = { it.id }) { message ->
                    MessageBubble(
                        message = message,
                        downloadState = state.downloads[message.id],
                        onDownload = { viewModel.downloadAttachment(message) },
                        onOpen = { file -> openFile(context, file, message.attachmentMime) },
                    )
                }
            }

            state.sendFileError?.let {
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(horizontal = 12.dp))
            }

            Composer(
                sending = state.sendingFile,
                uploadProgress = state.uploadProgress,
                onSend = { viewModel.send(it) },
                onSendFile = { uri, caption -> viewModel.sendFile(uri, caption) },
            )
        }
    }
}

private fun openFile(context: android.content.Context, file: java.io.File, mime: String?) {
    val resolvedMime = mime ?: MimeTypeMap.getSingleton().getMimeTypeFromExtension(file.extension) ?: "*/*"
    val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
    val intent = Intent(Intent.ACTION_VIEW).apply {
        setDataAndType(uri, resolvedMime)
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    }
    runCatching { context.startActivity(intent) }
}

@Composable
private fun MessageBubble(
    message: ChatMessage,
    downloadState: DownloadState?,
    onDownload: () -> Unit,
    onOpen: (java.io.File) -> Unit,
) {
    val isOut = message.direction == "out"
    // Telegram's actual bubble shape: rounded everywhere except the one
    // corner nearest the sender, which draws the "tail" corner tight.
    val bubbleShape = RoundedCornerShape(
        topStart = 16.dp, topEnd = 16.dp,
        bottomStart = if (isOut) 16.dp else 4.dp,
        bottomEnd = if (isOut) 4.dp else 16.dp,
    )
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = if (isOut) Arrangement.End else Arrangement.Start,
    ) {
        Surface(
            color = if (isOut) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant,
            contentColor = if (isOut) Color.White else MaterialTheme.colorScheme.onSurfaceVariant,
            shape = bubbleShape,
            shadowElevation = 1.dp,
            modifier = Modifier.widthIn(max = 280.dp),
        ) {
            Column(Modifier.padding(horizontal = 13.dp, vertical = 9.dp)) {
                if (message.text.isNotBlank()) {
                    Text(message.text, style = MaterialTheme.typography.bodyMedium)
                }
                if (message.attachmentPath != null) {
                    if (message.thumbnailPath != null) {
                        AsyncImage(
                            model = "$PLACEHOLDER_BASE_URL/api/chat/attachments/${message.id}/thumbnail",
                            contentDescription = message.attachmentName ?: "image",
                            modifier = Modifier
                                .padding(top = 4.dp)
                                .size(180.dp)
                                .clip(RoundedCornerShape(8.dp))
                                .clickable {
                                    when (downloadState) {
                                        is DownloadState.Ready -> onOpen(downloadState.file)
                                        else -> onDownload()
                                    }
                                },
                            contentScale = ContentScale.Crop,
                        )
                    }
                    AttachmentChip(
                        name = message.attachmentName ?: "file",
                        state = downloadState,
                        onClick = {
                            when (downloadState) {
                                is DownloadState.Ready -> onOpen(downloadState.file)
                                else -> onDownload()
                            }
                        },
                    )
                }
                Text(
                    message.username ?: message.platform,
                    style = MaterialTheme.typography.labelSmall,
                    color = LocalContentColor.current.copy(alpha = 0.65f),
                    modifier = Modifier.padding(top = 3.dp).fillMaxWidth(),
                    textAlign = if (isOut) TextAlign.End else TextAlign.Start,
                )
            }
        }
    }
}

@Composable
private fun AttachmentChip(name: String, state: DownloadState?, onClick: () -> Unit) {
    AssistChip(
        onClick = onClick,
        modifier = Modifier.padding(top = 4.dp),
        leadingIcon = {
            if (state is DownloadState.Downloading) CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
            else Icon(Icons.Filled.InsertDriveFile, contentDescription = null, modifier = Modifier.size(16.dp))
        },
        label = {
            Text(
                when (state) {
                    is DownloadState.Downloading -> "Downloading…"
                    is DownloadState.Ready -> name
                    is DownloadState.Error -> "$name — retry"
                    null -> "📎 $name"
                },
            )
        },
    )
}

@Composable
private fun Composer(sending: Boolean, uploadProgress: Float, onSend: (String) -> Unit, onSendFile: (Uri, String) -> Unit) {
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
        if (sending && uploadProgress > 0f) {
            LinearProgressIndicator(
                progress = { uploadProgress },
                modifier = Modifier.fillMaxWidth().padding(bottom = 6.dp),
            )
        }
        SlashCommandSuggestions(text = text, onSelect = { text = it })
        Row(verticalAlignment = Alignment.Bottom) {
            IconButton(onClick = { pickFile.launch("*/*") }, enabled = !sending) {
                Icon(Icons.Filled.AttachFile, contentDescription = "Attach a file")
            }
            // A pill-shaped, borderless field sitting in a tinted surface —
            // Telegram's composer, not Material's boxed OutlinedTextField.
            TextField(
                value = text,
                onValueChange = { text = it },
                modifier = Modifier.weight(1f),
                placeholder = { Text("Message…") },
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
                enabled = !sending && (text.isNotBlank() || pickedUri != null),
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
                if (sending) CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp, color = Color.White)
                else Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send")
            }
        }
    }
}
