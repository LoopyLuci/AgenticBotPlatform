package com.botserver.mobile.push

import android.app.PendingIntent
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.botserver.mobile.MainActivity
import com.botserver.mobile.R
import com.botserver.mobile.data.PushRepository
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import dagger.hilt.android.AndroidEntryPoint
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import javax.inject.Inject
import kotlin.random.Random

/**
 * Receives FCM pushes for new inbound bot messages (see bot/push.py's
 * notify_new_message) and refreshed-token callbacks, plus (see
 * bot/push.py's notify_apk_push) a data-only "apk_update" message the
 * instant an operator queues this device an APK push — that message
 * always reaches [onMessageReceived], even backgrounded, since it
 * carries no "notification" key for the OS to intercept and post
 * itself. Registered via Hilt so it can reach PushRepository/
 * UpdatePushSignal directly, same DI graph as the rest of the app.
 */
@AndroidEntryPoint
class FcmService : FirebaseMessagingService() {

    @Inject lateinit var pushRepository: PushRepository
    @Inject lateinit var updatePushSignal: UpdatePushSignal

    private val scope = CoroutineScope(Dispatchers.IO)

    override fun onNewToken(token: String) {
        scope.launch { pushRepository.registerToken(token) }
    }

    override fun onMessageReceived(message: RemoteMessage) {
        if (message.data["type"] == "apk_update") {
            handleApkUpdatePush(message.data["version_label"])
            return
        }
        val title = message.notification?.title ?: message.data["instance_name"] ?: "Bot Server"
        val body = message.notification?.body ?: return
        showNotification(title, body)
    }

    /** Wakes [com.botserver.mobile.data.PendingUpdateCoordinator] (alive
     * for the whole process, see its own doc) to start downloading right
     * away — covers the "app is open/backgrounded but process alive"
     * case. Also posts a real notification unconditionally: if the
     * process was killed entirely, nothing above ever runs until the
     * user taps this and relaunches the app, at which point the
     * coordinator's own eager check (see BotServerApp) picks the pending
     * push up regardless of whether the earlier signal was ever
     * delivered. */
    private fun handleApkUpdatePush(versionLabel: String?) {
        updatePushSignal.emit()
        showUpdateNotification(versionLabel)
    }

    private fun showUpdateNotification(versionLabel: String?) {
        ensureUpdateChannel()
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(this, UPDATE_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("BotServer update available")
            .setContentText(if (versionLabel.isNullOrBlank()) "Downloading now…" else "Downloading $versionLabel now…")
            .setAutoCancel(true)
            .setContentIntent(pendingIntent)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        NotificationManagerCompat.from(this).notify(UPDATE_NOTIFICATION_ID, notification)
    }

    private fun ensureUpdateChannel() {
        val channel = android.app.NotificationChannel(
            UPDATE_CHANNEL_ID, "App updates", android.app.NotificationManager.IMPORTANCE_HIGH,
        )
        getSystemService(android.app.NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun showNotification(title: String, body: String) {
        ensureChannel()
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(body)
            .setAutoCancel(true)
            .setContentIntent(pendingIntent)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        NotificationManagerCompat.from(this).notify(Random.nextInt(), notification)
    }

    private fun ensureChannel() {
        val channel = android.app.NotificationChannel(
            CHANNEL_ID, "New bot messages", android.app.NotificationManager.IMPORTANCE_HIGH,
        )
        getSystemService(android.app.NotificationManager::class.java).createNotificationChannel(channel)
    }

    companion object {
        const val CHANNEL_ID = "bot_messages"
        const val UPDATE_CHANNEL_ID = "app_updates"
        const val UPDATE_NOTIFICATION_ID = 918273
    }
}
