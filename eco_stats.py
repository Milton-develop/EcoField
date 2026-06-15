import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import numpy as np
from datetime import datetime

# --- PAGE CONFIG ---
st.set_page_config(
    page_title="EcoField Dashboard", 
    layout="wide", 
    page_icon="🌿",
    initial_sidebar_state="expanded"
)

st.title("EcoField Stats Dashboard")
st.markdown("""
**Biodiversity & Environmental Insights** from **EcoField Logger** data.
""")

# --- HELPER FUNCTIONS ---
def calculate_simpsons_index(counts):
    N = counts.sum()
    if N < 2:
        return 0.0
    numerator = sum(n * (n - 1) for n in counts)
    D = 1 - (numerator / (N * (N - 1)))
    return round(D, 3)

def calculate_shannon_index(counts):
    proportions = counts / counts.sum()
    proportions = proportions[proportions > 0]
    return round(-np.sum(proportions * np.log(proportions)), 3)

def calculate_evenness(shannon, richness):
    if richness <= 1:
        return 0.0
    return round(shannon / np.log(richness), 3)

# --- SIDEBAR ---
with st.sidebar:
    st.header("📤 Data Upload")
    uploaded_file = st.file_uploader("Upload Group Data (CSV)", type=['csv'])
    
    st.divider()
    st.caption("Made for EcoField Logger data")

# --- MAIN APP ---
if uploaded_file is not None:
    try:
        df = pd.read_csv(uploaded_file)
        df.columns = [c.lower().strip() for c in df.columns]

        # --- DATA PROCESSING ---
        processed_data = []

        for _, row in df.iterrows():
            # Standard + Manual entries
            species_list = str(row.get('species_list', '')).split(', ')
            count_list = str(row.get('count_list', '')).split(', ')
            manual_species = str(row.get('species_manual', '')).split(', ')
            manual_counts = str(row.get('count_manual', '')).split(', ')

            all_species = species_list + manual_species
            all_counts = count_list + manual_counts

            for name, count in zip(all_species, all_counts):
                name = str(name).strip()
                if name and name.lower() not in ['nan', 'none', '', 'other']:
                    try:
                        processed_data.append({
                            'group_id': row.get('group_id', 'N/A'),
                            'habitat': row.get('habitat', 'Unknown'),
                            'survey_type': row.get('survey_type', 'N/A'),
                            'species': name,
                            'count': int(float(count)) if count and str(count).lower() != 'nan' else 0,
                            'date': row.get('date', None),
                            'temperature': row.get('temperature'),
                            'humidity': row.get('humidity'),
                            'rainfall': row.get('rainfall'),
                            'wind_speed': row.get('wind_speed'),
                            'light_intensity': row.get('light_intensity'),
                            'canopy_cover': row.get('canopy_cover'),
                            'latitude': row.get('latitude'),
                            'longitude': row.get('longitude')
                        })
                    except:
                        continue

        long_df = pd.DataFrame(processed_data)

        if long_df.empty:
            st.error("No valid species data found in the uploaded file.")
            st.stop()

        # ====================== FILTERS ======================
        st.sidebar.header("🔍 Filters")
        
        habitats = sorted(long_df['habitat'].unique())
        selected_habitats = st.sidebar.multiselect("Filter Habitats", habitats, default=habitats)

        filtered_df = long_df[long_df['habitat'].isin(selected_habitats)]

        # ====================== TABS ======================
        tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
            "📊 Overview", 
            "🌱 Biodiversity", 
            "🐾 Species Analysis", 
            "🌡️ Environment", 
            "🗺️ Spatial", 
            "📋 Raw Data"
        ])

        # ==================== TAB 1: OVERVIEW ====================
        with tab1:
            st.subheader("Key Metrics")
            m1, m2, m3, m4, m5 = st.columns(5)
            
            m1.metric("Total Organisms", f"{filtered_df['count'].sum():,}")
            m2.metric("Species Richness", filtered_df['species'].nunique())
            m3.metric("Habitats Surveyed", filtered_df['habitat'].nunique())
            m4.metric("Active Groups", df['group_id'].nunique() if 'group_id' in df.columns else "N/A")
            m5.metric("Avg Individuals per Species", 
                     round(filtered_df['count'].sum() / filtered_df['species'].nunique(), 1))

            st.divider()

            col1, col2 = st.columns(2)
            with col1:
                st.subheader("Habitat Composition")
                hab_counts = filtered_df.groupby('habitat')['count'].sum().reset_index()
                fig_hab = px.bar(hab_counts, x='habitat', y='count', 
                                 color='habitat', color_discrete_sequence=px.colors.qualitative.Dark2)
                st.plotly_chart(fig_hab, use_container_width=True)

            with col2:
                st.subheader("Species Distribution")
                species_counts = filtered_df.groupby('species')['count'].sum().nlargest(15).reset_index()
                fig_pie = px.pie(species_counts, values='count', names='species', hole=0.45)
                st.plotly_chart(fig_pie, use_container_width=True)

        # ==================== TAB 2: BIODIVERSITY ====================
        with tab2:
            st.subheader("Diversity Indices by Habitat")

            diversity_data = []
            for hab in filtered_df['habitat'].unique():
                hab_df = filtered_df[filtered_df['habitat'] == hab]
                counts = hab_df['count']
                richness = hab_df['species'].nunique()
                
                simpson = calculate_simpsons_index(counts)
                shannon = calculate_shannon_index(counts)
                evenness = calculate_evenness(shannon, richness)

                diversity_data.append({
                    'Habitat': hab,
                    'Species Richness': richness,
                    "Simpson's Index": simpson,
                    "Shannon Index": shannon,
                    "Evenness": evenness
                })

            div_df = pd.DataFrame(diversity_data)
            
            col1, col2 = st.columns(2)
            with col1:
                fig_sim = px.bar(div_df, x='Habitat', y="Simpson's Index", 
                                 color="Simpson's Index", range_y=[0, 1],
                                 color_continuous_scale='Greens')
                st.plotly_chart(fig_sim, use_container_width=True)

            with col2:
                fig_sha = px.bar(div_df, x='Habitat', y="Shannon Index", 
                                 color="Shannon Index", color_continuous_scale='Viridis')
                st.plotly_chart(fig_sha, use_container_width=True)

            st.dataframe(div_df.round(3), use_container_width=True, hide_index=True)

        # ==================== TAB 3: SPECIES ANALYSIS ====================
        with tab3:
            st.subheader("Top Species")

            top_species = (filtered_df.groupby('species')['count']
                          .sum()
                          .reset_index()
                          .sort_values('count', ascending=False)
                          .head(20))

            col1, col2 = st.columns([3, 2])
            with col1:
                st.dataframe(top_species, use_container_width=True)

            with col2:
                fig_rank = px.bar(top_species.head(10), x='species', y='count', 
                                  title="Top 10 Species - Rank Abundance")
                st.plotly_chart(fig_rank, use_container_width=True)

            # Species by Habitat Heatmap
            st.subheader("Species × Habitat Abundance")
            pivot = filtered_df.pivot_table(
                index='species', 
                columns='habitat', 
                values='count', 
                aggfunc='sum',
                fill_value=0
            ).head(25)
            
            fig_heat = px.imshow(pivot, text_auto=True, aspect="auto", 
                                 color_continuous_scale='YlGnBu')
            st.plotly_chart(fig_heat, use_container_width=True)

        # ==================== TAB 4: ENVIRONMENT ====================
        with tab4:
            st.subheader("Microhabitat Environmental Variables")
            micro_vars = ['temperature', 'humidity', 'rainfall', 'wind_speed', 
                         'light_intensity', 'canopy_cover']
            
            avail_vars = [v for v in micro_vars if v in filtered_df.columns]

            if avail_vars:
                env_summary = filtered_df.groupby('habitat')[avail_vars].agg(['mean', 'std']).round(2)
                st.dataframe(env_summary, use_container_width=True)

                # Correlation Heatmap
                st.subheader("Environmental Variable Correlation")
                corr = filtered_df[avail_vars].corr()
                fig_corr = px.imshow(corr, text_auto=True, aspect="auto", 
                                    color_continuous_scale='RdBu', zmin=-1, zmax=1)
                st.plotly_chart(fig_corr, use_container_width=True)
            else:
                st.info("No environmental variables found in the dataset.")

        # ==================== TAB 5: SPATIAL ====================
        with tab5:
            if 'latitude' in filtered_df.columns and 'longitude' in filtered_df.columns:
                st.subheader("Field Observation Map")
                map_data = filtered_df[['latitude', 'longitude', 'habitat', 'species', 'count']].dropna()
                
                fig_map = px.scatter_mapbox(
                    map_data, lat='latitude', lon='longitude',
                    color='habitat',
                    size='count',
                    hover_data=['species', 'count'],
                    zoom=10,
                    mapbox_style="open-street-map"
                )
                fig_map.update_layout(height=600)
                st.plotly_chart(fig_map, use_container_width=True)
            else:
                st.info("Latitude and Longitude columns not found.")

        # ==================== TAB 6: RAW DATA ====================
        with tab6:
            st.subheader("Processed Data")
            st.dataframe(filtered_df, use_container_width=True)
            
            col1, col2 = st.columns(2)
            with col1:
                csv = filtered_df.to_csv(index=False).encode()
                st.download_button(
                    "📥 Download Processed Data", 
                    csv, 
                    "ecofields_processed.csv",
                    "text/csv"
                )
            with col2:
                st.download_button(
                    "📥 Download Original Data", 
                    uploaded_file.getvalue(),
                    "original_data.csv",
                    "text/csv"
                )

    except Exception as e:
        st.error(f"Error processing file: {e}")
        st.info("Please make sure you're uploading a valid EcoField Logger CSV.")
else:
    st.info("👆 Upload your EcoField Logger CSV file to begin analysis.")
    st.markdown("---")
    st.caption("Supports standard and manual species entries with environmental data.")