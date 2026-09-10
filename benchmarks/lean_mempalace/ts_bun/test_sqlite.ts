import { Database } from "bun:sqlite";
const db = new Database(process.env.MEMPALACE_DB_PATH!, { readonly: true });
const count = db.query("SELECT count(*) as count FROM documents").get() as any;
console.log("Document count from bun:sqlite:", count.count);
db.close();
