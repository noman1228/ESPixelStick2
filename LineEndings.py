import os

def convert_line_endings(directory):
    for root, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(root, file)

            # Skip binary files
            try:
                with open(file_path, 'rb') as f:
                    if b'\0' in f.read(1024):  # Check first 1024 bytes for null bytes
                        continue
            except Exception as e:
                print(f"Skipping {file_path}: {e}")
                continue

            # Read the file and convert line endings
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read().replace('\r\n', '\n')

                with open(file_path, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(content)

                print(f"Converted: {file_path}")

            except Exception as e:
                print(f"Error processing {file_path}: {e}")

if __name__ == "__main__":
    script_directory = os.path.dirname(os.path.abspath(__file__))
    convert_line_endings(script_directory)
