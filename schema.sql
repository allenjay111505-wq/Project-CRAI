CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  full_name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  student_id TEXT NOT NULL,
  course TEXT NOT NULL,
  year_level INTEGER NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('tutor','learner','both')),
  profile_photo TEXT,
  phone_number TEXT,
  sms_notifications_enabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  endpoint TEXT NOT NULL UNIQUE,
  subscription_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user
  ON push_subscriptions (user_id);
CREATE TABLE IF NOT EXISTS subjects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tutor_subjects (
  tutor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  PRIMARY KEY (tutor_id, subject_id)
);
CREATE TABLE IF NOT EXISTS availability (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tutor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
  day TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tutor_rates (
  tutor_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  rate_per_hour REAL  -- NULL = free
);
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_1_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  user_2_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (user_1_id < user_2_id),
  UNIQUE (user_1_id, user_2_id)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  content TEXT NOT NULL CHECK (length(trim(content)) > 0),
  sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
  read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_sent
  ON messages (conversation_id, sent_at, id);
CREATE INDEX IF NOT EXISTS idx_messages_unread
  ON messages (conversation_id, sender_id, read_at);
CREATE TABLE IF NOT EXISTS session_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tutor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  learner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  requested_subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
  scheduled_for TEXT,
  timezone_offset_minutes INTEGER,
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'accepted', 'declined', 'cancelled')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (tutor_id != learner_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_requests_pending_time
  ON session_requests (tutor_id, learner_id, scheduled_for)
  WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_session_requests_tutor_status
  ON session_requests (tutor_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_session_requests_learner_status
  ON session_requests (learner_id, status, created_at);
CREATE TABLE IF NOT EXISTS learning_resources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tutor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE RESTRICT,
  storage_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  file_name TEXT NOT NULL,
  file_type TEXT NOT NULL,
  file_size INTEGER NOT NULL CHECK (file_size > 0),
  is_published INTEGER NOT NULL DEFAULT 0 CHECK (is_published IN (0, 1)),
  uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_learning_resources_subject
  ON learning_resources (subject_id, is_published, uploaded_at);
CREATE INDEX IF NOT EXISTS idx_learning_resources_tutor
  ON learning_resources (tutor_id, uploaded_at);
CREATE TABLE IF NOT EXISTS quizzes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tutor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  resource_id INTEGER NOT NULL REFERENCES learning_resources(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
  focus_monitoring_enabled INTEGER NOT NULL DEFAULT 0
    CHECK (focus_monitoring_enabled IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_quizzes_resource_status
  ON quizzes (resource_id, status, created_at);
CREATE TABLE IF NOT EXISTS quiz_questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  test_number INTEGER NOT NULL CHECK (test_number IN (1, 2)),
  position INTEGER NOT NULL CHECK (position > 0),
  question TEXT NOT NULL,
  options_json TEXT NOT NULL DEFAULT '[]',
  answer_key TEXT NOT NULL,
  source_excerpt TEXT NOT NULL,
  UNIQUE (quiz_id, test_number, position)
);
CREATE TABLE IF NOT EXISTS quiz_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  learner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (quiz_id, learner_id)
);
CREATE TABLE IF NOT EXISTS quiz_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  learner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  submitted_at TEXT,
  focus_monitoring_enabled INTEGER NOT NULL DEFAULT 0
    CHECK (focus_monitoring_enabled IN (0, 1)),
  focus_consent INTEGER NOT NULL DEFAULT 0 CHECK (focus_consent IN (0, 1)),
  ai_grading_consent INTEGER NOT NULL DEFAULT 0 CHECK (ai_grading_consent IN (0, 1)),
  grading_method TEXT NOT NULL DEFAULT 'answer_key',
  answers_json TEXT NOT NULL DEFAULT '{}',
  score INTEGER,
  total_questions INTEGER
);
CREATE INDEX IF NOT EXISTS idx_quiz_attempts_tutor_results
  ON quiz_attempts (quiz_id, submitted_at, learner_id);
CREATE TABLE IF NOT EXISTS quiz_focus_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES quiz_attempts(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK (event_type IN ('hidden', 'blur')),
  occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quiz_focus_events_attempt
  ON quiz_focus_events (attempt_id, occurred_at);
CREATE TABLE IF NOT EXISTS session_quiz_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_request_id INTEGER NOT NULL REFERENCES session_requests(id) ON DELETE CASCADE,
  quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  attempt_id INTEGER UNIQUE REFERENCES quiz_attempts(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'waiting'
    CHECK (status IN ('waiting', 'started', 'submitted', 'released')),
  learner_ready INTEGER NOT NULL DEFAULT 0 CHECK (learner_ready IN (0, 1)),
  focus_consent INTEGER NOT NULL DEFAULT 0 CHECK (focus_consent IN (0, 1)),
  ai_grading_consent INTEGER NOT NULL DEFAULT 0 CHECK (ai_grading_consent IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (session_request_id, quiz_id)
);
CREATE INDEX IF NOT EXISTS idx_session_quiz_runs_session
  ON session_quiz_runs (session_request_id, created_at);
