"""Versioned Gemini policies shared unchanged by primary and fallback models."""

POLICY_VERSIONS = {
    "pre_dictionary": "dictionary-v5",
    "post_dictionary": "dictionary-v5",
    "translation": "translation-v9",
    "qa": "qa-v2",
    "repair": "repair-v2",
}

DICTIONARY = """Resolve terminology for a Chinese-to-Vietnamese novel translation. RAW Chinese establishes identity; the supplied locked dictionary establishes existing canonical translations and may never be challenged. Confirm only reusable, clearly evidenced named identities or stable terms. Establish identity before selecting Vietnamese wording and attach proven alternate forms to their canonical owner. Use project-consistent Sino-Vietnamese forms for native Chinese identities. When evidence clearly establishes a foreign identity transliterated into Chinese, restore its recognized international/Vietnamese name (for example 费利佩二世 -> Felipe II); never output Pinyin. Leave uncertain identity or wording unresolved. Never add action fragments, generic words, pronouns, temporary noun phrases, ordinary idioms, or grammatical substrings. Never rewrite a locked mapping.

Return a JSON object with confirmed, rejected, and unresolved arrays. A rejected record is {"source":"...","reason":"generic phrase, fragment, or no reusable identity"}; use it only when the candidate is clearly not a glossary entry. An unresolved record is {"source":"...","possible_type":"character|location|institution|book_title|term|unknown","reason":"..."}; use it only when the candidate plausibly is a glossary entry but the supplied evidence is not enough. Every requested candidate must appear in exactly one outcome array. Every confirmed operation must be exactly one of:
{"operation":"add_entry","entry":{"source":"邱途","translation":"Khâu Đồ","type":"character","gender":"male","status":"locked","aliases":[]}}
{"operation":"add_alias","canonical_source":"邱途","alias":{"source":"邱探员","translation":"Thám viên Khâu"}}
Types: character, location, institution, book_title, term. Use institution for named offices and organizations; term for stable concepts, objects, abilities, and techniques. Genders: male, female, unknown, not_applicable. Use not_applicable for non-person entities when known. Every confirmed entry status is exactly locked. If identity, Vietnamese wording, owner, or type is uncertain, use unresolved rather than guessing. Return only JSON."""

TRANSLATE = """Dịch Trung→Việt đầy đủ, mượt và dễ đọc nhưng giữ sắc thái hội thoại, văn hóa và quan hệ nhân vật của nguyên tác; không Việt hóa quá mức. RAW quyết định nghĩa, LOCKED DICTIONARY quyết định tên/thuật ngữ. Không thêm/bớt/tóm tắt/đoán, không bám cứng cú pháp Trung; giữ đúng chủ thể, quan hệ, sắc thái, phủ định, số liệu và thứ tự.

Không tự chèn thói quen nói tiếng Việt như ạ, dạ, vâng, nhé, nha, đấy, cơ, mà chỉ để câu nghe tự nhiên. Chỉ dùng khi RAW thực sự thể hiện sắc thái tương đương. Đặc biệt không biến mọi câu kính ngữ thành câu kết thúc bằng ạ; thể hiện tôn ti chủ yếu qua đại từ, danh xưng, chức vị và cách hành văn.

Dùng tiếng Việt trung tính thiên miền Nam, tránh Bắc hóa (bố→cha/ba, bát→chén, cốc→ly, thìa→muỗng, ngô→bắp, lợn→heo) nhưng cũng không Nam hóa bằng ổng/bả/ảnh/cổ/tụi bây nếu nguyên tác không có sắc thái đó. Với cổ trang/lịch sử, giữ cách nói phù hợp bối cảnh Trung Quốc và thân phận nhân vật.

Tên/thuật ngữ phải nhất quán; alias quy đúng identity; không overwrite dictionary; cuối batch trả full dictionary đã merge. Chỉ dùng mục trong LOCKED DICTIONARY; không tự tạo, khóa, sửa, xóa hay ghi đè mục từ. Mục chưa đủ bằng chứng không được xem là đã khóa. Không để sót CJK trong tiếng Việt.

Trả đúng một segment tiếng Việt hoàn chỉnh cho mỗi CURRENT_RAW ID, giữ nguyên ID và thứ tự; không gộp/tách segment, chuyển thông tin giữa ID, dịch PREVIOUS_TRANSLATION hoặc NEXT_SOURCE. Dịch title khi không rỗng, chỉ trả nội dung title, không thêm số chương/heading. Author/platform note phải được dịch trung thực như ghi chú.

Ứng dụng chịu trách nhiệm merge từ điển trước/sau batch và xuất full dictionary đã merge ở cuối batch; không đưa dictionary vào response dịch. Chỉ trả JSON theo schema được cung cấp."""

REPAIR = """Repair the confirmed defect in exactly one Chinese-to-Vietnamese paragraph. RAW is semantic truth and supplied locked terminology is absolute. Return the same paragraph ID and ONE COMPLETE corrected Vietnamese paragraph—not a changed phrase, excerpt, diff, or fragment. Preserve all accepted surrounding wording and every unrelated piece of meaning. Vietnamese decimal commas and source decimal periods may express the same value. Return only the structured JSON schema."""

QA = """Conservatively compare only the supplied suspicious RAW paragraphs with their Vietnamese translations. Report an evidence-supported semantic defect only for omission, mistranslation, reversed meaning/negation, changed number, wrong speaker or subject/object, hallucination, untranslated source, locked terminology, or obvious truncation. Different wording, shorter natural Vietnamese, reordered syntax, natural pronoun omission, clause splitting/combining, or an alternative style is not a defect. A low length ratio alone is not proof. Return an empty findings array when there is no real defect. Locked dictionary mappings are mandatory and may not be challenged. Return only the structured JSON schema."""
