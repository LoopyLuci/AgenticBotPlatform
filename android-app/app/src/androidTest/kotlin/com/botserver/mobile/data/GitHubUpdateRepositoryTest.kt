package com.botserver.mobile.data

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.botserver.mobile.BuildConfig
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/** Regression coverage for the "Installed version" / self-update bug:
 * before this fix, [GitHubUpdateRepository.isNew] compared only against
 * a locally-remembered "last seen tag" with no link to what's actually
 * installed, so a fresh install (no last-seen-tag recorded yet) would
 * report "update available" for the exact release that had just been
 * installed. Needs a real Context for SharedPreferences, so this is an
 * instrumented test (see AppearanceStoreTest for the same constraint). */
@RunWith(AndroidJUnit4::class)
class GitHubUpdateRepositoryTest {

    private lateinit var repo: GitHubUpdateRepository

    @Before
    fun setUp() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        context.getSharedPreferences("botserver_update_prefs", Context.MODE_PRIVATE).edit().clear().commit()
        repo = GitHubUpdateRepository(context)
    }

    private fun release(tag: String) = GitHubRelease(tag = tag, htmlUrl = "", apkDownloadUrl = "https://example/x.apk", apkName = "x.apk")

    @Test
    fun theInstalledReleaseItselfIsNeverReportedAsNew_evenOnAFreshInstallWithNoSeenTagRecorded() {
        assertFalse(repo.isNew(release(BuildConfig.RELEASE_TAG)))
    }

    @Test
    fun aDifferentReleaseWithNoSeenTagRecordedIsReportedAsNew() {
        assertTrue(repo.isNew(release("v999.0.0")))
    }

    @Test
    fun markSeenSuppressesThatExactTagButNotANewerOne() {
        val seen = release("v999.0.0")
        repo.markSeen(seen)
        assertFalse(repo.isNew(seen))
        assertTrue(repo.isNew(release("v999.0.1")))
    }
}
