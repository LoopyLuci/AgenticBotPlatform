package com.botserver.mobile.data.db

import androidx.room.Database
import androidx.room.RoomDatabase

@Database(entities = [BotEntity::class, ChatMessageEntity::class], version = 2, exportSchema = false)
abstract class AppDatabase : RoomDatabase() {
    abstract fun botDao(): BotDao
    abstract fun chatDao(): ChatDao
}
