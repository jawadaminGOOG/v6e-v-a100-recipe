import zipfile
import xml.etree.ElementTree as ET
import os

def parse_xlsx(file_path):
    with zipfile.ZipFile(file_path, 'r') as zip_ref:
        shared_strings = []
        try:
            with zip_ref.open('xl/sharedStrings.xml') as f:
                tree = ET.parse(f)
                root = tree.getroot()
                ns = {'ns': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                for sst in root.findall('.//ns:t', ns):
                    shared_strings.append(sst.text)
        except KeyError:
            pass

        with zip_ref.open('xl/worksheets/sheet1.xml') as f:
            tree = ET.parse(f)
            root = tree.getroot()
            ns = {'ns': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            
            rows = []
            for row_el in root.findall('.//ns:row', ns):
                row_data = []
                for cell_el in row_el.findall('ns:c', ns):
                    val = ""
                    if cell_el.get('t') == 'inlineStr':
                        inline_t = cell_el.find('.//ns:t', ns)
                        if inline_t is not None:
                            val = inline_t.text
                    else:
                        val_el = cell_el.find('ns:v', ns)
                        if val_el is not None:
                            val = val_el.text
                            if cell_el.get('t') == 's':
                                idx = int(val)
                                val = shared_strings[idx] if idx < len(shared_strings) else f"StringIndexError({idx})"
                    row_data.append(val)
                rows.append(row_data)
    return rows

parts_dir = "/usr/local/google/home/jawadamin/.gemini/jetski/brain/8efd12ab-10c2-4f4b-acf5-833de551e91d/g4_multinode_results/exp_2_rerun/parts"

concurrencies = [100, 250, 500, 1000]
runs = [1, 2]

print("System | Concurrency | Run | Aggregated RPS | Avg p50 (sec) | Avg p99 (sec) | Total Success | Total Failed")
print("-" * 110)

for C in concurrencies:
    for r in runs:
        total_rps = 0.0
        p50_list = []
        p99_list = []
        total_success = 0
        total_failed = 0
        
        valid_parts = 0
        for i in range(1, 5):
            filename = f"C{C}_R{r}_Part{i}.xlsx"
            filepath = os.path.join(parts_dir, filename)
            if not os.path.exists(filepath):
                continue
                
            rows = parse_xlsx(filepath)
            if len(rows) < 2:
                continue
                
            headers = rows[0]
            data = rows[1] # there is only one data row per file
            
            # Map headers to indices
            indices = {h: idx for idx, h in enumerate(headers)}
            
            try:
                rps = float(data[indices['RPS']])
                p50 = float(data[indices['Latency p50 (sec)']])
                p99 = float(data[indices['Latency p99 (sec)']])
                success = int(data[indices['Total Successful Requests']])
                failed = int(data[indices['Failed Requests']])
                
                total_rps += rps
                p50_list.append(p50)
                p99_list.append(p99)
                total_success += success
                total_failed += failed
                valid_parts += 1
            except (KeyError, ValueError, IndexError) as e:
                print(f"Error parsing {filename}: {e}")
                
        if valid_parts == 4:
            avg_p50 = sum(p50_list) / len(p50_list)
            avg_p99 = sum(p99_list) / len(p99_list)
            print(f"G4-LMC-Rerun | {C} | {r} | {total_rps:.2f} | {avg_p50:.4f} | {avg_p99:.4f} | {total_success} | {total_failed}")
        else:
            print(f"G4-LMC-Rerun | {C} | {r} | Incomplete ({valid_parts}/4 parts found)")
