import { Database } from "bun:sqlite";
const db = new Database("C:/Users/igorl/.mempalace/palace/sqlite_exact.sqlite3", { readonly: true });
const count = db.query("SELECT count(*) as count FROM documents").get() as any;
console.log("Document count from bun:sqlite:", count.count);
db.close();
