# Converts the legacy product CSV into a local SQLite database.
import pandas as pd
import sqlite3


db_path = 'db.sqlite'
csv_path = 'flipkart_product_data.csv'


conn = sqlite3.connect(db_path)
cursor = conn.cursor()


cursor.execute('''
CREATE TABLE IF NOT EXISTS product (
    product_link TEXT,
    title TEXT,
    brand TEXT,
    price INTEGER,
    avg_rating FLOAT,
    total_ratings INTEGER
);
''')


conn.commit()


df = pd.read_csv(csv_path)


df.to_sql('product', conn, if_exists='append', index=False)


conn.close()

print("Data inserted successfully!")
