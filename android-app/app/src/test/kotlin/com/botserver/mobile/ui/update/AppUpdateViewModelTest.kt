package com.botserver.mobile.ui.update

import com.botserver.mobile.data.GitHubRelease
import com.botserver.mobile.data.GitHubUpdateRepository
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

/** AppUpdateViewModel — the self-update state machine shared by
 * PairingScreen (reachable with no pairing at all) and Settings. This
 * update path is deliberately independent of the paired BotServer
 * connection (see GitHubUpdateRepository's doc), so it's tested against
 * a mocked repository rather than a real HTTP call. */
class AppUpdateViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun buildViewModel(gitHub: GitHubUpdateRepository) =
        AppUpdateViewModel(gitHub, mockk<UpdateRepository>(relaxed = true))

    @Test
    fun `checkForUpdate reports Available for a release not yet seen`() = runTest {
        val release = GitHubRelease("v0.5.0", "https://github.com/x", "https://example.com/app.apk", "app.apk")
        val gitHub = mockk<GitHubUpdateRepository> {
            coEvery { checkLatest() } returns release
            every { isNew(release) } returns true
        }
        val viewModel = buildViewModel(gitHub)

        viewModel.checkForUpdate()
        assertEquals(AppUpdateState.Checking, viewModel.state.value)
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(AppUpdateState.Available(release), viewModel.state.value)
    }

    @Test
    fun `checkForUpdate reports UpToDate for an already-seen release`() = runTest {
        val release = GitHubRelease("v0.5.0", "https://github.com/x", "https://example.com/app.apk", "app.apk")
        val gitHub = mockk<GitHubUpdateRepository> {
            coEvery { checkLatest() } returns release
            every { isNew(release) } returns false
        }
        val viewModel = buildViewModel(gitHub)

        viewModel.checkForUpdate()
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(AppUpdateState.UpToDate, viewModel.state.value)
    }

    @Test
    fun `checkForUpdate reports Error when the network call fails`() = runTest {
        val gitHub = mockk<GitHubUpdateRepository> {
            coEvery { checkLatest() } throws java.io.IOException("no connection")
        }
        val viewModel = buildViewModel(gitHub)

        viewModel.checkForUpdate()
        dispatcher.scheduler.advanceUntilIdle()

        assertTrue(viewModel.state.value is AppUpdateState.Error)
    }

    @Test
    fun `downloadAndInstall marks the release seen and reports Downloaded`() = runTest {
        val release = GitHubRelease("v0.5.0", "https://github.com/x", "https://example.com/app.apk", "app.apk")
        val file = File("app.apk")
        val gitHub = mockk<GitHubUpdateRepository> {
            coEvery { checkLatest() } returns release
            every { isNew(release) } returns true
            coEvery { download(release) } returns file
            every { markSeen(release) } returns Unit
        }
        val viewModel = buildViewModel(gitHub)
        viewModel.checkForUpdate()
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.downloadAndInstall()
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(AppUpdateState.Downloaded(file), viewModel.state.value)
    }

    @Test
    fun `downloadAndInstall does nothing when no update is available`() = runTest {
        val gitHub = mockk<GitHubUpdateRepository>(relaxed = true)
        val viewModel = buildViewModel(gitHub)

        viewModel.downloadAndInstall()
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(AppUpdateState.Idle, viewModel.state.value)
    }

    @Test
    fun `dismiss marks an available release seen and returns to Idle`() = runTest {
        val release = GitHubRelease("v0.5.0", "https://github.com/x", "https://example.com/app.apk", "app.apk")
        val gitHub = mockk<GitHubUpdateRepository> {
            coEvery { checkLatest() } returns release
            every { isNew(release) } returns true
            every { markSeen(release) } returns Unit
        }
        val viewModel = buildViewModel(gitHub)
        viewModel.checkForUpdate()
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.dismiss()

        assertEquals(AppUpdateState.Idle, viewModel.state.value)
    }
}
