package com.botserver.mobile.di

import android.content.Context
import androidx.room.Room
import com.botserver.mobile.data.db.AppDatabase
import com.botserver.mobile.data.db.BotDao
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
        Room.databaseBuilder(context, AppDatabase::class.java, "botserver.db").build()

    @Provides
    @Singleton
    fun provideBotDao(db: AppDatabase): BotDao = db.botDao()
}
