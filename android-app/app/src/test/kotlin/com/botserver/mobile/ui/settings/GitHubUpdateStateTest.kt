package com.botserver.mobile.ui.settings

import com.botserver.mobile.data.CredentialStore
import com.botserver.mobile.data.GitHubRelease
import com.botserver.mobile.data.GitHubUpdateRepository
import com.botserver.mobile.data.SettingsRepository
import com.botserver.mobile.data.UpdateRepository
import io.mockk.coEvery
import io.mockk.every
import io.mockk.mockk
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** SettingsViewModel's GitHub self-update state machine
 * (checkForGitHubUpdate/downloadGitHubUpdate) — this update path is
 * deliberately independent of the paired BotServer connection (see
 * GitHubUpdateRepository's doc), so it's tested against a mocked
 * repository rather than a real HTTP call. */
class GitHubUpdateStateTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun buildViewModel(gitHub: GitHubUpdateRepository) = SettingsViewModel(
        repository = mockk<SettingsRepository>(relaxed = true),
        credentials = mockk<CredentialStore>(relaxed = true),
        gitHubUpdateRepository = gitHub,
        updateRepository = mockk<UpdateRepository>(relaxed = true),
    )

    @Test
    fun `checkForGitHubUpdate reports Available for a release not yet seen`() = runTest {
        val release = GitHubRelease("v0.5.0", "https://github.com/x", "https://example.com/app.apk", "app.apk")
        val gitHub = mockk<GitHubUpdateRepository> {
            coEvery { checkLatest() } returns release
            every { isNew(release) } returns true
        }
        val viewModel = buildViewModel(gitHub)

        viewModel.checkForGitHubUpdate()
        assertEquals(GitHubUpdateState.Checking, viewModel.gitHubUpdateState.value)
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(GitHubUpdateState.Available(release), viewModel.gitHubUpdateState.value)
    }

    @Test
    fun `checkForGitHubUpdate reports UpToDate for an already-seen release`() = runTest {
        val release = GitHubRelease("v0.5.0", "https://github.com/x", "https://example.com/app.apk", "app.apk")
        val gitHub = mockk<GitHubUpdateRepository> {
            coEvery { checkLatest() } returns release
            every { isNew(release) } returns false
        }
        val viewModel = buildViewModel(gitHub)

        viewModel.checkForGitHubUpdate()
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(GitHubUpdateState.UpToDate, viewModel.gitHubUpdateState.value)
    }

    @Test
    fun `checkForGitHubUpdate reports Error when the network call fails`() = runTest {
        val gitHub = mockk<GitHubUpdateRepository> {
            coEvery { checkLatest() } throws java.io.IOException("no connection")
        }
        val viewModel = buildViewModel(gitHub)

        viewModel.checkForGitHubUpdate()
        dispatcher.scheduler.advanceUntilIdle()

        assertTrue(viewModel.gitHubUpdateState.value is GitHubUpdateState.Error)
    }

    @Test
    fun `downloadGitHubUpdate marks the release seen and reports Downloaded`() = runTest {
        val release = GitHubRelease("v0.5.0", "https://github.com/x", "https://example.com/app.apk", "app.apk")
        val file = File("app.apk")
        val gitHub = mockk<GitHubUpdateRepository> {
            coEvery { checkLatest() } returns release
            every { isNew(release) } returns true
            coEvery { download(release) } returns file
            every { markSeen(release) } returns Unit
        }
        val viewModel = buildViewModel(gitHub)
        viewModel.checkForGitHubUpdate()
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.downloadGitHubUpdate()
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(GitHubUpdateState.Downloaded(file), viewModel.gitHubUpdateState.value)
    }

    @Test
    fun `downloadGitHubUpdate does nothing when no update is available`() = runTest {
        val gitHub = mockk<GitHubUpdateRepository>(relaxed = true)
        val viewModel = buildViewModel(gitHub)

        viewModel.downloadGitHubUpdate()
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(GitHubUpdateState.Idle, viewModel.gitHubUpdateState.value)
    }
}
