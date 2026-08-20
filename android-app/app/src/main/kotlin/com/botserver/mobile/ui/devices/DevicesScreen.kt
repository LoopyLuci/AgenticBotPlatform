package com.botserver.mobile.ui.devices

import android.content.Intent
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import androidx.hilt.navigation.compose.hiltViewModel
import com.botserver.mobile.data.dto.DeviceInfo
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/** Lets an already-paired device onboard a *new* one without a PC: share
 * this app's own APK (any OS share target — Bluetooth, Nearby Share, etc.)
 * and hand over a freshly minted pairing key, as a scannable QR or a
 * shareable botserver://pair link. Mirrors the dashboard's Mobile tab. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DevicesScreen(viewModel: DevicesViewModel = hiltViewModel()) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val state by viewModel.state.collectAsState()
    val devices by viewModel.devices.collectAsState()
    val refreshing by viewModel.refreshing.collectAsState()
    var label by remember { mutableStateOf("") }
    var apkShareError by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(Unit) { viewModel.startPresence() }

    Scaffold(topBar = { TopAppBar(title = { Text("Devices") }) }) { padding ->
        Column(
            modifier = Modifier.fillMaxSize().padding(padding).padding(20.dp).verticalScroll(rememberScrollState()),
        ) {
            Surface(shape = RoundedCornerShape(12.dp), tonalElevation = 1.dp, modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text("Connected devices", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                        TextButton(onClick = { viewModel.refreshDevices() }, enabled = !refreshing) {
                            if (refreshing) CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp)
                            else Text("Update Devices")
                        }
                    }
                    Text(
                        "Every device paired to this server, loaded from the desktop app's device list — tap \"Update Devices\" if one you just paired or expect to see online isn't showing up yet.",
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(top = 4.dp, bottom = 12.dp),
                    )
                    if (devices.isEmpty()) {
                        Text("No other devices paired yet.", style = MaterialTheme.typography.bodySmall)
                    } else {
                        devices.forEach { device -> DeviceRow(device) }
                    }
                }
            }

            Spacer(Modifier.height(16.dp))

            Surface(shape = RoundedCornerShape(12.dp), tonalElevation = 1.dp, modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Text("Share this app", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                    Text(
                        "Send the installed app itself to a new device over Bluetooth, Nearby Share, or any share target.",
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(top = 4.dp, bottom = 12.dp),
                    )
                    Button(onClick = {
                        apkShareError = null
                        scope.launch {
                            runCatching { shareApk(context) }
                                .onFailure { e -> apkShareError = e.message ?: "Couldn't share the app." }
                        }
                    }) {
                        Text("Share app")
                    }
                    apkShareError?.let {
                        Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(top = 8.dp))
                    }
                }
            }

            Spacer(Modifier.height(16.dp))

            Surface(shape = RoundedCornerShape(12.dp), tonalElevation = 1.dp, modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Text("Pair a new device", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                    Text(
                        "Generates a fresh key using this device's own connection — no host to type by hand.",
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(top = 4.dp, bottom = 12.dp),
                    )
                    OutlinedTextField(
                        value = label,
                        onValueChange = { label = it },
                        label = { Text("Device label, e.g. \"My Other Phone\"") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        enabled = state !is GenerateState.Generating,
                    )
                    Spacer(Modifier.height(12.dp))
                    Button(
                        onClick = { viewModel.generate(label) },
                        enabled = state !is GenerateState.Generating,
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        if (state is GenerateState.Generating) CircularProgressIndicator(modifier = Modifier.size(18.dp))
                        else Text("Generate key for a new device")
                    }

                    when (val s = state) {
                        is GenerateState.Error -> Text(
                            s.message,
                            color = MaterialTheme.colorScheme.error,
                            modifier = Modifier.padding(top = 12.dp),
                        )
                        is GenerateState.Ready -> {
                            Spacer(Modifier.height(16.dp))
                            Text("Shown once — copy or share it now.", color = MaterialTheme.colorScheme.tertiary, style = MaterialTheme.typography.labelSmall)
                            Spacer(Modifier.height(8.dp))
                            Image(
                                bitmap = s.pairing.qr.asImageBitmap(),
                                contentDescription = "Pairing QR code",
                                modifier = Modifier.size(200.dp).align(Alignment.CenterHorizontally),
                            )
                            Spacer(Modifier.height(12.dp))
                            SelectionContainer {
                                Text(
                                    s.pairing.key,
                                    style = MaterialTheme.typography.bodySmall,
                                    modifier = Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(8.dp)).padding(10.dp),
                                )
                            }
                            Spacer(Modifier.height(12.dp))
                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                OutlinedButton(onClick = {
                                    val intent = Intent(Intent.ACTION_SEND).apply {
                                        type = "text/plain"
                                        putExtra(Intent.EXTRA_TEXT, s.pairing.pairUri())
                                    }
                                    context.startActivity(Intent.createChooser(intent, "Share pairing link"))
                                }) { Text("Share pairing link") }
                                TextButton(onClick = { viewModel.reset(); label = "" }) { Text("Done") }
                            }
                        }
                        else -> {}
                    }
                }
            }
        }
    }
}

@Composable
private fun DeviceRow(device: DeviceInfo) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(9.dp)
                .clip(CircleShape)
                .background(if (device.online) MaterialTheme.colorScheme.tertiary else MaterialTheme.colorScheme.outlineVariant),
        )
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            Text(device.label, style = MaterialTheme.typography.bodyMedium)
            Text(
                // Real hardware identity when the device has reported one
                // (device_model/os_version — see NetworkModule.kt's
                // X-Device-Model/X-Device-OS-Version headers) takes priority
                // over the bare platform string, which is only a fallback
                // for older presence rows recorded before this existed.
                listOfNotNull(
                    device.deviceModel ?: device.platform,
                    device.osVersion,
                    if (device.online) "Online" else "Offline",
                ).joinToString(" · "),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** Copies this app's own installed APK into the cache dir (its system
 * install path isn't directly shareable) and hands out a content:// URI
 * for that copy via FileProvider — reuses whatever nearby-share mechanism
 * the OS already offers instead of building a custom transfer. */
private suspend fun shareApk(context: android.content.Context) {
    val destApk = withContext(Dispatchers.IO) {
        val sourceApk = File(context.applicationInfo.sourceDir)
        val shareDir = File(context.cacheDir, "shared").apply { mkdirs() }
        val dest = File(shareDir, "BotServer.apk")
        sourceApk.copyTo(dest, overwrite = true)
        dest
    }
    val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", destApk)
    val intent = Intent(Intent.ACTION_SEND).apply {
        type = "application/vnd.android.package-archive"
        putExtra(Intent.EXTRA_STREAM, uri)
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    }
    context.startActivity(Intent.createChooser(intent, "Share Bot Server app"))
}
