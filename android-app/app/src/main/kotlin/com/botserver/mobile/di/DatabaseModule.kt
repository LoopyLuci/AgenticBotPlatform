package com.botserver.mobile.di

import android.content.Context
import androidx.room.Room
import com.botserver.mobile.data.db.AppDatabase
import com.botserver.mobile.data.db.BotDao
import com.botserver.mobile.data.db.ChatDao
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object DatabaseModule {
    @Provides
    @Singleton
    fun provideAppDatabase(@ApplicationContext context: Context): AppDatabase =
        Room.databaseBuilder(context, AppDatabase::class.java, "botserver.db")
            // Everything in this database is a disposable, reconstructable
            // cache of what the server already has — never the sole copy of
            // anything — so a destructive fallback on a schema bump is a
            // deliberate simplification, not data-loss risk, while this
            // cache is still actively growing new entities.
            .fallbackToDestructiveMigration()
            .build()

    @Provides
    @Singleton
    fun provideBotDao(db: AppDatabase): BotDao = db.botDao()

    @Provides
    @Singleton
    fun provideChatDao(db: AppDatabase): ChatDao = db.chatDao()
}
