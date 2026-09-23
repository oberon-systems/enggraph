export interface User {
  id: string;
  email: string;
  salt: string;
  passwordHash: string;
  disabled: boolean;
}

export interface Database {
  one<T>(sql: string, params: unknown[]): Promise<T | null>;
  run(sql: string, params: unknown[]): Promise<void>;
}

export class UserRepository {
  constructor(private readonly db: Database) {}

  findByEmail(email: string): Promise<User | null> {
    return this.db.one<User>("SELECT * FROM users WHERE email = $1", [email]);
  }

  findById(id: string): Promise<User | null> {
    return this.db.one<User>("SELECT * FROM users WHERE id = $1", [id]);
  }

  disable(id: string): Promise<void> {
    return this.db.run("UPDATE users SET disabled = true WHERE id = $1", [id]);
  }
}
